//! Preparing a drive: everything Museion needs, laid out to run from wherever
//! the drive comes up (see docs/portable.md).
//!
//! Reuses the pieces that exist -- the catalogue, the verified downloader,
//! the tool installer, the indexer -- and adds only the layout, the choice
//! of what to carry, and the record of what was carried. Every download is
//! resumable and hash-checked, so a drive can be prepared over several
//! sittings and a half-written file is never mistaken for a whole one.

use crate::catalog::{self, Model, Source};
use crate::download::{self, Spec};
use crate::{disk, indexer, tools, IndexOptions};
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

/// Microsoft's "Evergreen Standalone Installer", x64. Unhashed: Microsoft
/// republishes it under the same link as the runtime updates.
pub const WEBVIEW2_URL: &str = "https://go.microsoft.com/fwlink/p/?LinkId=2124701";
pub const WEBVIEW2_FILE: &str = "MicrosoftEdgeWebView2RuntimeInstallerX64.exe";
const WEBVIEW2_ESTIMATE: u64 = 170_000_000;

/// The marker file that makes a folder a portable root.
pub const MARKER: &str = "museion-portable";

/// Port a temporary kiwix-serve uses while the index is built on the drive,
/// well away from the running app's.
const INDEX_PORT: u16 = 8095;

fn yes() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Plan {
    pub dest: PathBuf,
    /// Bundle id from the catalogue.
    pub bundle: String,
    /// Chat model ids from the catalogue. The reranker is always carried.
    pub models: Vec<String>,
    /// llama.cpp builds to carry: cuda, vulkan, cpu, metal.
    pub accelerators: Vec<String>,
    /// The app to put on the drive as Museion.exe.
    #[serde(default)]
    pub app_exe: Option<PathBuf>,
    /// The installer to put in support\, for people who would rather install.
    #[serde(default)]
    pub installer: Option<PathBuf>,
    #[serde(default = "yes")]
    pub build_index: bool,
    #[serde(default = "yes")]
    pub webview2: bool,
    /// Everything except content and models: the release skeleton. The
    /// first launch from it shows the first-run screen and fetches the
    /// bundle and a model into the drive.
    #[serde(default)]
    pub skeleton: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct Estimate {
    pub bytes: u64,
    /// Bytes still to fetch, allowing for what is already in place.
    pub remaining: u64,
    pub free_bytes: u64,
    pub items: Vec<(String, u64)>,
}

#[derive(Debug, Clone)]
pub enum Progress {
    Phase(String),
    Download { item: String, received: u64, total: u64 },
    Note(String),
}

/// What the drive will hold and whether it fits.
pub fn estimate(plan: &Plan) -> Result<Estimate> {
    let mut items: Vec<(String, u64)> = Vec::new();
    let mut remaining = 0u64;
    let (_, sources) = catalog::bundle(&plan.bundle)?;
    let sources: Vec<Source> = if plan.skeleton { Vec::new() } else { sources };
    for s in &sources {
        items.push((s.title.clone(), s.size_bytes));
        if !present(&plan.dest.join("library").join(&s.filename), s.size_bytes) {
            remaining += s.size_bytes;
        }
    }
    for m in if plan.skeleton { Vec::new() } else { selected_models(plan)? } {
        items.push((m.title.clone(), m.size_bytes));
        if !present(&plan.dest.join("models").join(&m.filename), m.size_bytes) {
            remaining += m.size_bytes;
        }
    }
    for t in catalog::tools()? {
        if t.name == "llama.cpp" {
            for a in &plan.accelerators {
                let Some((_, v)) = catalog::tool_variant_named(&t, a) else {
                    bail!("no llama.cpp build for {a:?} on this platform");
                };
                let mut size = v.size_bytes;
                for k in &v.also {
                    if let Some(c) = t.variants.get(k) {
                        size += c.size_bytes;
                    }
                }
                items.push((format!("llama.cpp ({a})"), size));
                if !plan.dest.join("bin").join("llama").join(a).exists() {
                    remaining += size;
                }
            }
        } else if let Some((_, v)) = catalog::tool_variant_named(&t, "cpu") {
            items.push((t.name.clone(), v.size_bytes));
            if !plan.dest.join("bin").exists() {
                remaining += v.size_bytes;
            }
        }
    }
    if plan.webview2 {
        items.push(("WebView2 runtime installer".into(), WEBVIEW2_ESTIMATE));
        if !plan.dest.join("support").join(WEBVIEW2_FILE).exists() {
            remaining += WEBVIEW2_ESTIMATE;
        }
    }
    for (label, p) in [("Museion.exe", &plan.app_exe), ("installer", &plan.installer)] {
        if let Some(p) = p {
            let n = std::fs::metadata(p).map(|m| m.len()).unwrap_or(0);
            items.push((label.into(), n));
            remaining += n;
        }
    }
    let bytes = items.iter().map(|i| i.1).sum();
    let free_bytes = disk::free_space(&nearest_existing(&plan.dest)).unwrap_or(0);
    Ok(Estimate { bytes, remaining, free_bytes, items })
}

fn present(path: &Path, size: u64) -> bool {
    std::fs::metadata(path).map(|m| m.len() == size).unwrap_or(false)
}

fn nearest_existing(p: &Path) -> PathBuf {
    let mut cur = p.to_path_buf();
    while !cur.exists() {
        match cur.parent() {
            Some(parent) => cur = parent.to_path_buf(),
            None => break,
        }
    }
    cur
}

/// The chat models named in the plan, checked against the catalogue.
fn selected_models(plan: &Plan) -> Result<Vec<Model>> {
    let all = catalog::models()?;
    let mut out = Vec::new();
    for id in &plan.models {
        let m = all
            .iter()
            .find(|m| &m.id == id)
            .ok_or_else(|| anyhow!("no model {id:?} in the catalogue"))?;
        if m.role != "chat" {
            bail!("{id} is not a chat model");
        }
        out.push(m.clone());
    }
    if out.is_empty() && !plan.skeleton {
        bail!("choose at least one chat model");
    }
    if let Some(r) = catalog::reranker_model()? {
        out.push(r);
    }
    Ok(out)
}

/// Build the drive. Returns the root. Safe to run again: everything already
/// in place and correct is skipped.
pub fn run(plan: &Plan, mut report: impl FnMut(Progress)) -> Result<PathBuf> {
    let root = plan.dest.clone();
    let est = estimate(plan)?;
    // A gigabyte of headroom for the index and whatever the app writes.
    let headroom = 1u64 << 30;
    if est.free_bytes < est.remaining + headroom {
        bail!(
            "{} needs {} more; {} has {} free",
            root.display(),
            human(est.remaining + headroom),
            nearest_existing(&root).display(),
            human(est.free_bytes)
        );
    }

    report(Progress::Phase("Laying out the drive".into()));
    let library = root.join("library");
    let models = root.join("models");
    let bin = root.join("bin");
    let support = root.join("support");
    for d in [&library, &models, &bin, &support, &support.join("LICENSES")] {
        std::fs::create_dir_all(d).with_context(|| format!("creating {}", d.display()))?;
    }
    let marker = root.join(MARKER);
    if !marker.exists() {
        let id = format!("drive-{:x}", seed());
        std::fs::write(&marker, format!("{id}\n"))?;
    }

    report(Progress::Phase("Helper programs".into()));
    let mut on_tool = |s: tools::Step| match s {
        tools::Step::Downloading { tool, file, received, total } => {
            report(Progress::Download { item: format!("{tool}: {file}"), received, total })
        }
        tools::Step::Unpacking { tool } => report(Progress::Note(format!("Unpacking {tool}"))),
        tools::Step::Done { tool } => report(Progress::Note(format!("{tool} ready"))),
        tools::Step::Skipped { tool, reason } => report(Progress::Note(format!("{tool}: {reason}"))),
    };
    if tools::find_exe(&bin, "kiwix-serve").is_none() {
        tools::install_selected("cpu", &bin, &["kiwix-tools", "pdfium"], &mut on_tool)?;
    }
    for a in &plan.accelerators {
        let dir = bin.join("llama").join(a);
        if tools::find_exe(&dir, "llama-server").is_none() {
            tools::install_selected(a, &dir, &["llama.cpp"], &mut on_tool)?;
        }
    }

    report(Progress::Phase("Models".into()));
    let chosen = selected_models(plan)?;
    for m in chosen.iter().filter(|_| !plan.skeleton) {
        let target = models.join(&m.filename);
        if download::is_verified(&target, m.size_bytes, &m.sha256)? {
            report(Progress::Note(format!("{} already there", m.title)));
            continue;
        }
        let title = m.title.clone();
        download::download_verified(
            &Spec { urls: &m.urls, dest: &target, size_bytes: m.size_bytes, sha256: &m.sha256 },
            |p| report(Progress::Download { item: title.clone(), received: p.received, total: p.total }),
        )?;
    }

    report(Progress::Phase("Archives".into()));
    let (bundle, sources) = catalog::bundle(&plan.bundle)?;
    for s in sources.iter().filter(|_| !plan.skeleton) {
        let target = library.join(&s.filename);
        if download::is_verified(&target, s.size_bytes, &s.sha256)? {
            report(Progress::Note(format!("{} already there", s.title)));
            continue;
        }
        let urls = s.all_urls();
        let title = s.title.clone();
        download::download_verified(
            &Spec { urls: &urls, dest: &target, size_bytes: s.size_bytes, sha256: &s.sha256 },
            |p| report(Progress::Download { item: title.clone(), received: p.received, total: p.total }),
        )?;
    }

    report(Progress::Phase("The app".into()));
    if let Some(exe) = &plan.app_exe {
        let target = root.join("Museion.exe");
        if !same_file(exe, &target) {
            std::fs::copy(exe, &target).with_context(|| format!("copying {}", exe.display()))?;
        }
    }
    if let Some(inst) = &plan.installer {
        let name = inst.file_name().map(|n| n.to_os_string()).unwrap_or_else(|| "Museion-setup.exe".into());
        let target = support.join(name);
        if !same_file(inst, &target) {
            std::fs::copy(inst, &target).with_context(|| format!("copying {}", inst.display()))?;
        }
    }
    if plan.webview2 {
        let target = support.join(WEBVIEW2_FILE);
        if !target.exists() {
            download::fetch_unverified(WEBVIEW2_URL, &target, |received, total| {
                report(Progress::Download { item: "WebView2 runtime installer".into(), received, total })
            })?;
        }
    }

    report(Progress::Phase("Configuration".into()));
    write_config(plan, &chosen, &root)?;
    write_licenses(&support.join("LICENSES"), &sources, &chosen)?;
    write_readme(&root, &bundle.title)?;

    if plan.build_index && !plan.skeleton {
        report(Progress::Phase("Building the index".into()));
        build_index(&root, &library, &bin, &mut report)?;
    }

    report(Progress::Phase("Verifying".into()));
    write_manifest(&root, &sources, &chosen, plan)?;
    report(Progress::Note("Done.".into()));
    Ok(root)
}

fn same_file(a: &Path, b: &Path) -> bool {
    match (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
        (Ok(x), Ok(y)) => x == y,
        _ => false,
    }
}

fn seed() -> u64 {
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    t ^ ((std::process::id() as u64) << 32)
}

fn human(bytes: u64) -> String {
    let g = bytes as f64 / 1_073_741_824.0;
    if g >= 1.0 {
        format!("{g:.1} GB")
    } else {
        format!("{} MB", bytes / 1_048_576)
    }
}

/// The drive's config: relative paths, the smallest chosen model and the
/// CPU build as defaults. The first launch on any computer replaces both
/// with what fits it (portable mode's machine profile).
fn write_config(plan: &Plan, chosen: &[Model], root: &Path) -> Result<()> {
    let exe = |stem: &str| if cfg!(windows) { format!("{stem}.exe") } else { stem.to_string() };
    let default_accel = plan
        .accelerators
        .iter()
        .find(|a| a.as_str() == "cpu")
        .or(plan.accelerators.first())
        .cloned()
        .unwrap_or_else(|| "cpu".into());
    // A skeleton names the catalogue's smallest chat model so the first
    // run's set-up knows what to fetch; nothing is on the drive yet.
    let fallback = catalog::models()?
        .into_iter()
        .filter(|m| m.role == "chat")
        .min_by_key(|m| m.size_bytes);
    let chat = chosen
        .iter()
        .filter(|m| m.role == "chat")
        .min_by_key(|m| m.size_bytes)
        .cloned()
        .or(fallback)
        .ok_or_else(|| anyhow!("no chat model chosen"))?;
    let chat = &chat;
    let reranker = chosen.iter().find(|m| m.role != "chat");
    let cfg = serde_json::json!({
        "library_dir": "library",
        "extra_dirs": [],
        "kiwix_exe": format!("bin/{}", exe("kiwix-serve")),
        "llama_exe": format!("bin/llama/{default_accel}/{}", exe("llama-server")),
        "pdfium_dir": "bin",
        "models_dir": "models",
        "chat_model": format!("models/{}", chat.filename),
        "reranker_model": reranker.map(|r| format!("models/{}", r.filename)),
        "chat_args": chat.server_args,
        "chat_gpu_layers": 999,
        "kiwix_port": 8090,
        "chat_port": 8080,
        "reranker_port": 8081,
        "autostart": true
    });
    std::fs::write(root.join("config.json"), serde_json::to_string_pretty(&cfg)?)?;
    Ok(())
}

fn write_licenses(dir: &Path, sources: &[Source], models: &[Model]) -> Result<()> {
    let mut text = String::from("What this drive carries, and under what terms.\n\n");
    text.push_str("Content\n");
    for s in sources {
        let (name, attr) = s
            .license
            .as_ref()
            .map(|l| (l.name.as_str(), l.attribution.as_str()))
            .unwrap_or(("(see the archive's own metadata)", ""));
        text.push_str(&format!("  {} -- {}{}\n", s.title, name, if attr.is_empty() { String::new() } else { format!(" -- {attr}") }));
    }
    text.push_str("\nModels\n");
    for m in models {
        let (name, attr) = m
            .license
            .as_ref()
            .map(|l| (l.name.as_str(), l.attribution.as_str()))
            .unwrap_or(("(see the model card)", ""));
        text.push_str(&format!("  {} -- {}{}\n", m.title, name, if attr.is_empty() { String::new() } else { format!(" -- {attr}") }));
    }
    text.push_str("\nHelper programs\n");
    for t in catalog::tools()? {
        let (name, attr) = t
            .license
            .as_ref()
            .map(|l| (l.name.as_str(), l.attribution.as_str()))
            .unwrap_or(("(see the project)", ""));
        text.push_str(&format!("  {} -- {}{}\n", t.name, name, if attr.is_empty() { String::new() } else { format!(" -- {attr}") }));
    }
    text.push_str("\nMuseion itself -- see the repository's LICENSE.\n");
    std::fs::write(dir.join("README.txt"), text)?;
    Ok(())
}

fn write_readme(root: &Path, bundle: &str) -> Result<()> {
    let text = format!(
        "Museion -- an offline reference library with an assistant that answers from it.\n\
         \n\
         This drive carries the {bundle} collection, the language models, and the helper\n\
         programs. Nothing on it needs the internet.\n\
         \n\
         To use it from the drive:  double-click Museion.exe.\n\
         The first start on a computer measures its graphics card and picks the model\n\
         that fits; that takes a minute and happens once per computer.\n\
         \n\
         If the window does not open, the computer lacks the WebView2 runtime that\n\
         Windows normally ships with: run support\\{webview} once, then try again.\n\
         \n\
         To install it instead:  run the installer in support\\, then copy the library\n\
         and models folders somewhere local and point Settings at them. Running from a\n\
         flash drive means the model is read from it on every start; a copy on the\n\
         computer's own disk is much faster.\n\
         \n\
         support\\LICENSES\\README.txt says what is on the drive and under what terms.\n\
         MANIFEST.yaml lists every file with its checksum; Settings can verify them.\n",
        webview = WEBVIEW2_FILE
    );
    std::fs::write(root.join("README.txt"), text)?;
    Ok(())
}

/// Build the index on the drive, with a kiwix-serve of its own for the
/// duration: the indexer reads archive content over HTTP, because zimdump
/// corrupts binaries on Windows.
fn build_index(root: &Path, library: &Path, bin: &Path, report: &mut impl FnMut(Progress)) -> Result<()> {
    let kiwix = tools::find_exe(bin, "kiwix-serve").ok_or_else(|| anyhow!("kiwix-serve missing from {}", bin.display()))?;
    let mut zims: Vec<PathBuf> = std::fs::read_dir(library)?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e.eq_ignore_ascii_case("zim")))
        .collect();
    zims.sort();
    if zims.is_empty() {
        return Ok(());
    }
    let mut cmd = Command::new(&kiwix);
    cmd.arg("--port").arg(INDEX_PORT.to_string());
    for z in &zims {
        cmd.arg(z);
    }
    cmd.stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let mut server = Guard(cmd.spawn().context("starting kiwix-serve for indexing")?);
    let url = format!("http://127.0.0.1:{INDEX_PORT}");
    let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(3)).build();
    let started = Instant::now();
    while agent.get(&format!("{url}/catalog/v2/entries")).call().is_err() {
        if started.elapsed() > Duration::from_secs(60) {
            bail!("kiwix-serve did not come up on port {INDEX_PORT}");
        }
        if let Ok(Some(_)) = server.0.try_wait() {
            bail!("kiwix-serve exited while starting");
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    let db = library.join("museion-index.db");
    let opts = IndexOptions { library, extra_dirs: &[], db: &db, kiwix_url: &url, pdfium_dir: bin, force: false };
    let stats = indexer::index_library(&opts, |p| match p {
        indexer::Progress::Archive { book, documents } => {
            report(Progress::Note(format!("Indexing {book}: {documents} documents")))
        }
        indexer::Progress::Document { book, done, total, .. } => {
            report(Progress::Download { item: format!("Indexing {book}"), received: done as u64, total: total as u64 })
        }
        _ => {}
    })?;
    report(Progress::Note(format!(
        "Indexed {} documents, {} pages, {} words",
        stats.documents, stats.pages, stats.words
    )));
    let _ = root;
    Ok(())
}

struct Guard(Child);
impl Drop for Guard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// Everything catalogued on the drive, with its hash, so the drive can be
/// checked years later. Hashes come from the catalogue; each file was
/// verified against them on the way in.
fn write_manifest(root: &Path, sources: &[Source], models: &[Model], plan: &Plan) -> Result<()> {
    let mut y = String::new();
    y.push_str("# Museion drive manifest. Every file below was verified against its sha256 when\n");
    y.push_str("# the drive was prepared; `museion verify` (or Settings) checks them again.\n");
    y.push_str(&format!("bundle: {}\n", plan.bundle));
    y.push_str(&format!("accelerators: [{}]\n", plan.accelerators.join(", ")));
    y.push_str("files:\n");
    for s in sources {
        y.push_str(&format!("  - path: library/{}\n    bytes: {}\n    sha256: {}\n", s.filename, s.size_bytes, s.sha256));
    }
    for m in models {
        y.push_str(&format!("  - path: models/{}\n    bytes: {}\n    sha256: {}\n", m.filename, m.size_bytes, m.sha256));
    }
    std::fs::write(root.join("MANIFEST.yaml"), y)?;
    Ok(())
}
