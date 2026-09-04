//! Museion desktop: the shell around the retrieval core and its sidecars.

mod config;
mod sidecar;

use config::Config;
use museion_core::{Asker, Index, Llama};
use serde::Serialize;
use sidecar::{Role, Supervisor};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{Emitter, Manager, State};

pub struct AppState {
    config: Mutex<Config>,
    config_path: PathBuf,
    supervisor: Supervisor,
    /// Detected once. Detection shells out to nvidia-smi, and doing that on
    /// the main thread every time the Library tab opened is what made the
    /// page scroll like it was wading.
    hardware: std::sync::OnceLock<museion_core::Hardware>,
    /// Index totals, cached. Opening SQLite and summing every four seconds for
    /// three numbers in the header was the other half of the jank.
    stats: Mutex<Option<(std::time::Instant, (usize, usize, usize))>>,
}

impl AppState {
    fn hardware(&self) -> Result<museion_core::Hardware, String> {
        if let Some(h) = self.hardware.get() {
            return Ok(h.clone());
        }
        let h = museion_core::detect_hardware().map_err(ui_err)?;
        let _ = self.hardware.set(h.clone());
        Ok(h)
    }

    /// (documents, pages, words), refreshed at most every 30 s or when told.
    fn index_stats(&self, cfg: &Config) -> (usize, usize, usize) {
        if let Ok(guard) = self.stats.lock() {
            if let Some((at, v)) = *guard {
                if at.elapsed() < Duration::from_secs(30) {
                    return v;
                }
            }
        }
        let v = match cfg.index_path() {
            Some(p) if p.exists() => Index::open(&p)
                .and_then(|i| i.stats())
                .map(|s| (s.documents, s.pages, s.words))
                .unwrap_or((0, 0, 0)),
            _ => (0, 0, 0),
        };
        if let Ok(mut guard) = self.stats.lock() {
            *guard = Some((std::time::Instant::now(), v));
        }
        v
    }

    fn forget_stats(&self) {
        if let Ok(mut guard) = self.stats.lock() {
            *guard = None;
        }
    }
}

/// Errors cross the boundary to the UI as strings; anyhow's chain is preserved
/// so the cause is not lost on the way.
type UiResult<T> = Result<T, String>;

fn ui_err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

#[derive(Serialize)]
pub struct Status {
    configured: bool,
    library_dir: Option<String>,
    documents: usize,
    pages: usize,
    words: usize,
    kiwix_running: bool,
    chat_running: bool,
    reranker_running: bool,
    chat_model: Option<String>,
    reranker_model: Option<String>,
}

#[derive(Serialize)]
pub struct HardwareInfo {
    accelerator: String,
    memory_mb: u64,
    system_ram_mb: u64,
    tier: String,
    tier_detail: String,
}

#[derive(Serialize, Clone)]
pub struct Source {
    n: usize,
    title: String,
    author: String,
    page: usize,
    book: String,
    /// PDF filename for a page source; article path for a native archive.
    /// What the Browse tab needs to open the thing itself.
    file: String,
    snippet: String,
}

#[derive(Serialize)]
pub struct AskResult {
    sources: Vec<Source>,
    /// Where the answer came from, judged from which tools actually ran.
    basis: museion_core::Basis,
    /// Every calculation actually performed -- shown as ground truth.
    calculations: Vec<museion_core::Calculation>,
}

fn describe_hardware(hw: &museion_core::Hardware) -> HardwareInfo {
    use museion_core::Accelerator;
    let (accelerator, memory_mb) = match &hw.accelerator {
        Accelerator::Cuda { name, vram_mb } => (format!("{name} (CUDA)"), *vram_mb),
        Accelerator::Metal { name, unified_mb } => (format!("{name} (Metal)"), *unified_mb),
        Accelerator::Vulkan { name, vram_mb } => (format!("{name} (Vulkan)"), *vram_mb),
        Accelerator::Cpu => ("No usable GPU — CPU only".into(), 0),
    };
    HardwareInfo {
        accelerator,
        memory_mb,
        system_ram_mb: hw.system_ram_mb,
        tier: format!("{:?}", hw.tier),
        tier_detail: hw.tier.describe().to_string(),
    }
}

#[tauri::command]
async fn get_hardware(state: State<'_, AppState>) -> UiResult<HardwareInfo> {
    Ok(describe_hardware(&state.hardware()?))
}

/// Just the service flags, for the header dots. Cheap enough to poll: it
/// touches the supervisor's process list and nothing else.
#[derive(Serialize)]
pub struct ServiceState {
    kiwix_running: bool,
    chat_running: bool,
    reranker_running: bool,
}

#[tauri::command]
fn service_state(state: State<AppState>) -> UiResult<ServiceState> {
    Ok(ServiceState {
        kiwix_running: state.supervisor.is_running(Role::Kiwix),
        chat_running: state.supervisor.is_running(Role::Chat),
        reranker_running: state.supervisor.is_running(Role::Reranker),
    })
}

#[tauri::command]
async fn get_status(state: State<'_, AppState>) -> UiResult<Status> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let (documents, pages, words) = state.index_stats(&cfg);
    Ok(Status {
        configured: cfg.is_ready(),
        library_dir: cfg.library_dir.as_ref().map(|p| p.display().to_string()),
        documents,
        pages,
        words,
        kiwix_running: state.supervisor.is_running(Role::Kiwix),
        chat_running: state.supervisor.is_running(Role::Chat),
        reranker_running: state.supervisor.is_running(Role::Reranker),
        chat_model: cfg.chat_model.as_ref().map(|p| file_name(p)),
        reranker_model: cfg.reranker_model.as_ref().map(|p| file_name(p)),
    })
}

fn file_name(p: &std::path::Path) -> String {
    p.file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| p.display().to_string())
}

#[tauri::command]
fn get_config(state: State<AppState>) -> UiResult<Config> {
    Ok(state.config.lock().map_err(ui_err)?.clone())
}

#[tauri::command]
fn set_config(state: State<AppState>, config: Config) -> UiResult<()> {
    config.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = config;
    Ok(())
}

/// Bring up the sidecars. Reports progress as events so the UI can say what it
/// is waiting for -- a large model can take minutes, and silence during that
/// looks like a hang.
#[tauri::command]
async fn start_services(app: tauri::AppHandle, state: State<'_, AppState>) -> UiResult<()> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let kiwix_exe = cfg.kiwix_exe.clone().ok_or("kiwix-serve path not configured")?;
    let llama_exe = cfg.llama_exe.clone().ok_or("llama-server path not configured")?;
    let library = cfg.library_dir.clone().ok_or("no library configured")?;

    let say = |msg: &str| {
        let _ = app.emit("service-progress", msg.to_string());
    };

    let _ = &library;
    say("Starting the content server…");
    state
        .supervisor
        .start_kiwix(&kiwix_exe, &cfg.content_dirs(), cfg.kiwix_port)
        .map_err(ui_err)?;
    state
        .supervisor
        .wait_ready(
            Role::Kiwix,
            &format!("http://127.0.0.1:{}/catalog/v2/entries", cfg.kiwix_port),
            Duration::from_secs(60),
        )
        .map_err(ui_err)?;

    if let Some(model) = &cfg.reranker_model {
        say("Starting the reranker…");
        // GPU layers deliberately 0: on a 12 GB card the chat model wants all of
        // it, and this costs only seconds on CPU.
        let args: Vec<String> = ["--reranking", "--pooling", "rank", "--batch-size", "2048",
                                 "--ubatch-size", "2048", "--ctx-size", "8192"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        state
            .supervisor
            .start_llama(
                Role::Reranker,
                &llama_exe,
                model,
                cfg.reranker_port,
                0,
                &args,
            )
            .map_err(ui_err)?;
        state
            .supervisor
            .wait_ready(
                Role::Reranker,
                &format!("http://127.0.0.1:{}/v1/models", cfg.reranker_port),
                Duration::from_secs(180),
            )
            .map_err(ui_err)?;
    }

    let model = cfg.chat_model.clone().ok_or("no chat model configured")?;
    say("Loading the chat model — this can take a few minutes…");
    let mut args = cfg.chat_args.clone();
    for a in ["--flash-attn", "on", "--jinja", "--parallel", "1",
              "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"] {
        args.push(a.to_string());
    }
    state
        .supervisor
        .start_llama(
            Role::Chat,
            &llama_exe,
            &model,
            cfg.chat_port,
            cfg.chat_gpu_layers,
            &args,
        )
        .map_err(ui_err)?;
    state
        .supervisor
        .wait_ready(
            Role::Chat,
            &format!("http://127.0.0.1:{}/v1/models", cfg.chat_port),
            Duration::from_secs(600),
        )
        .map_err(ui_err)?;

    say("Ready.");
    Ok(())
}

#[tauri::command]
fn stop_services(state: State<AppState>) -> UiResult<()> {
    state.supervisor.stop_all();
    Ok(())
}

/// Ask a question. Answer tokens arrive as "answer-token" events; the sources
/// come back as the return value once generation finishes.
#[tauri::command]
async fn ask(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    question: String,
) -> UiResult<AskResult> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let index_path = cfg.index_path().ok_or("no library configured")?;
    let index = Index::open(&index_path).map_err(ui_err)?;

    let chat = Llama::new(&format!("http://127.0.0.1:{}", cfg.chat_port));
    let reranker = Llama::new(&format!("http://127.0.0.1:{}", cfg.reranker_port));

    // Say so at once rather than letting the question wait on a server that
    // is absent or still loading. The distinction matters to the person: one
    // means press Start, the other means wait.
    // Arithmetic is answered without a model; only insist on one otherwise.
    if !museion_core::calc::looks_like_arithmetic(&question) && !chat.is_up() {
        return Err(if state.supervisor.is_running(Role::Chat) {
            "The chat model is still loading. Give it a minute and try again.".into()
        } else {
            "The chat model isn't running. Open the Library tab and press Start services.".into()
        });
    }
    let use_rerank = reranker.is_up();

    // Archives the local index does not hold are searched through
    // kiwix-serve's own index -- that is how Wikipedia becomes askable.
    let kiwix = museion_core::Kiwix::new(&format!("http://127.0.0.1:{}", cfg.kiwix_port));
    let kiwix_up = state.supervisor.is_running(Role::Kiwix) && kiwix.is_up();
    let native_books: Vec<String> = if kiwix_up {
        let processed = index.processed_books().unwrap_or_default();
        cfg.content_dirs()
            .iter()
            .filter_map(|d| std::fs::read_dir(d).ok())
            .flat_map(|rd| {
                rd.flatten()
                    .filter_map(|e| e.file_name().to_string_lossy().strip_suffix(".zim").map(str::to_string))
                    .collect::<Vec<_>>()
            })
            .filter(|b| !processed.iter().any(|p| p == b))
            .collect()
    } else {
        Vec::new()
    };

    let asker = Asker {
        index: &index,
        chat: &chat,
        reranker: if use_rerank { Some(&reranker) } else { None },
        kiwix: if kiwix_up { Some(&kiwix) } else { None },
        native_books,
    };

    let mut streamed = false;
    let answer = asker
        .ask(&question, |t| match t {
            museion_core::Token::Text(s) => {
                streamed = true;
                let _ = app.emit("answer-token", s.to_string());
            }
            museion_core::Token::Discard => {
                streamed = false;
                let _ = app.emit("answer-reset", ());
            }
        })
        .map_err(ui_err)?;

    // Only if nothing reached the UI at all. This used to key on "no sources",
    // which was right when the only sourceless answer was the refusal and
    // wrong once computed and general-knowledge answers stream with none --
    // every one of those arrived twice.
    if !streamed && !answer.text.is_empty() {
        let _ = app.emit("answer-token", answer.text.clone());
    }

    Ok(AskResult {
        sources: answer
            .sources
            .iter()
            .enumerate()
            .map(|(i, h)| Source {
                n: i + 1,
                title: h.doc_title.clone(),
                author: h.author.clone(),
                page: h.page,
                book: h.book.clone(),
                file: h.file.clone(),
                snippet: h.snippet.clone(),
            })
            .collect(),
        basis: answer.basis,
        calculations: answer.calculations,
    })
}

/// Build the search index over the library. Needs the content server running,
/// which start_services arranges. Progress goes out as "index-progress" events
/// -- 155 documents take a few minutes, and a person should see which one it
/// is on rather than a frozen button.
#[tauri::command]
async fn build_index(app: tauri::AppHandle, state: State<'_, AppState>, force: bool) -> UiResult<String> {
    use museion_core::{indexer::Outcome, Progress};
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let library = cfg.library_dir.clone().ok_or("no library configured")?;
    let db = cfg.index_path().ok_or("no library configured")?;
    let pdfium = cfg
        .pdfium_dir
        .clone()
        .or_else(|| cfg.kiwix_exe.as_ref().and_then(|p| p.parent().map(|d| d.to_path_buf())))
        .ok_or("pdfium directory not configured")?;
    if !state.supervisor.is_running(Role::Kiwix) {
        return Err("the content server is not running -- start services first".into());
    }
    let kiwix_url = format!("http://127.0.0.1:{}", cfg.kiwix_port);

    let say = |msg: String| {
        let _ = app.emit("index-progress", msg);
    };
    let opts = museion_core::IndexOptions {
        library: &library,
        extra_dirs: &cfg.extra_dirs,
        db: &db,
        kiwix_url: &kiwix_url,
        pdfium_dir: &pdfium,
        force,
    };
    let stats = museion_core::index_library(&opts, |p| match p {
        Progress::Archive { book, documents } => say(format!("Indexing {book} ({documents} documents)…")),
        Progress::Native { book } => say(format!("{book}: uses its own index")),
        Progress::Skipped { book } => say(format!("{book}: already indexed")),
        Progress::Document { done, total, title, outcome, .. } => {
            if outcome != Outcome::Duplicate {
                say(format!("{done}/{total}  {title}"));
            }
        }
        Progress::ArchiveDone { book, indexed, .. } => say(format!("{book}: {indexed} documents indexed")),
    })
    .map_err(ui_err)?;

    state.forget_stats();
    Ok(format!(
        "{} documents, {} pages, {} words indexed.",
        stats.documents, stats.pages, stats.words
    ))
}

/// Measure the chat model against this machine and store the offload split.
#[tauri::command]
async fn fit_chat_model(state: State<'_, AppState>) -> UiResult<String> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let model = cfg.chat_model.clone().ok_or("no chat model configured")?;
    let llama = cfg.llama_exe.clone().ok_or("llama-server path not configured")?;
    let fit_exe = llama
        .parent()
        .map(|d| d.join(if cfg!(windows) { "llama-fit-params.exe" } else { "llama-fit-params" }))
        .ok_or("cannot locate llama-fit-params next to llama-server")?;

    // Context comes from whatever --ctx-size is already configured, so the
    // fit is measured for the configuration that will actually run.
    let ctx = cfg
        .chat_args
        .windows(2)
        .find(|w| w[0] == "--ctx-size" || w[0] == "-c")
        .and_then(|w| w[1].parse().ok())
        .unwrap_or(32768);

    // The fit measures free GPU memory. With the chat model already resident
    // the card reads as nearly full and the split comes out far too cautious:
    // measured here, blocks 10-40 offloaded instead of 15-40. So the model is
    // stopped first, and the person is told to start services again.
    let was_running = state.supervisor.is_running(Role::Chat);
    if was_running {
        state.supervisor.stop(Role::Chat);
        std::thread::sleep(Duration::from_secs(2));
    }
    let fit = museion_core::fit_model(&fit_exe, &model, ctx).map_err(ui_err)?;

    // Keep any non-fit arguments the user had (like --ctx-size), replace the
    // fitted ones. The fit already includes -c, so drop ours to avoid a repeat.
    let mut args: Vec<String> = Vec::new();
    let mut skip = false;
    for a in &cfg.chat_args {
        if skip {
            skip = false;
            continue;
        }
        if a == "--ctx-size" || a == "-c" || a == "-ngl" || a == "--n-gpu-layers" || a == "-ot" {
            skip = true;
            continue;
        }
        args.push(a.clone());
    }
    let mut fitted = fit.args.clone();
    // -ngl is passed by the supervisor from chat_gpu_layers; strip it here.
    let mut i = 0;
    while i < fitted.len() {
        if fitted[i] == "-ngl" || fitted[i] == "--n-gpu-layers" {
            fitted.drain(i..(i + 2).min(fitted.len()));
        } else {
            i += 1;
        }
    }
    args.extend(fitted);

    let mut updated = cfg.clone();
    updated.chat_args = args;
    if let Some(n) = fit.gpu_layers {
        updated.chat_gpu_layers = n;
    }
    updated.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = updated;

    Ok(format!(
        "{} layers on the GPU; {}",
        fit.gpu_layers.map(|n| n.to_string()).unwrap_or_else(|| "all".into()),
        if fit.args.iter().any(|a| a == "-ot") {
            "expert tensors split between GPU and CPU."
        } else {
            "everything resident on the GPU."
        }
    ))
}

// ----------------------------------------------------------------- setup

#[derive(Serialize)]
pub struct PlanItem {
    id: String,
    title: String,
    size_bytes: u64,
}

#[derive(Serialize)]
pub struct SetupPlan {
    hardware: HardwareInfo,
    chat_model: Option<PlanItem>,
    reranker: Option<PlanItem>,
    tools: Vec<PlanItem>,
    bundles: Vec<PlanItem>,
}

/// What a first run on this machine would fetch, and how big it is, so the
/// person can decide before anything downloads.
#[tauri::command]
async fn setup_plan(state: State<'_, AppState>) -> UiResult<SetupPlan> {
    use museion_core::catalog;
    let hw = state.hardware()?;
    let hardware = describe_hardware(&hw);
    let chat_model = catalog::chat_model_for(hw.tier, hw.system_ram_mb)
        .map_err(ui_err)?
        .map(|m| PlanItem { id: m.id, title: m.title, size_bytes: m.size_bytes });
    let reranker = catalog::reranker_model()
        .map_err(ui_err)?
        .map(|m| PlanItem { id: m.id, title: m.title, size_bytes: m.size_bytes });
    let tools = museion_core::tools::plan(&hw.accelerator)
        .map_err(ui_err)?
        .into_iter()
        .map(|(name, key, size)| PlanItem { id: key, title: name, size_bytes: size })
        .collect();
    let mut bundles = Vec::new();
    for b in catalog::bundles().map_err(ui_err)? {
        let (_, srcs) = catalog::bundle(&b.id).map_err(ui_err)?;
        bundles.push(PlanItem {
            id: b.id,
            title: b.title,
            size_bytes: srcs.iter().map(|s| s.size_bytes).sum(),
        });
    }
    Ok(SetupPlan { hardware, chat_model, reranker, tools, bundles })
}

#[derive(Serialize, Clone)]
struct FetchProgress {
    item: String,
    received: u64,
    total: u64,
    note: String,
}

fn emit_progress(app: &tauri::AppHandle, item: &str, received: u64, total: u64, note: &str) {
    let _ = app.emit(
        "fetch-progress",
        FetchProgress { item: item.into(), received, total, note: note.into() },
    );
}

/// Download the helper programs into `dest` and point the config at them.
#[tauri::command]
async fn fetch_tools(app: tauri::AppHandle, state: State<'_, AppState>, dest: String) -> UiResult<String> {
    use museion_core::tools::{self, Step};
    let hw = state.hardware()?;
    let dest = PathBuf::from(dest);
    let app2 = app.clone();
    let installed = tauri::async_runtime::spawn_blocking(move || {
        tools::install(&hw.accelerator, &dest, |s| match s {
            Step::Downloading { tool, file, received, total } => {
                emit_progress(&app2, &tool, received, total, &file)
            }
            Step::Unpacking { tool } => emit_progress(&app2, &tool, 0, 0, "unpacking"),
            Step::Done { tool } => emit_progress(&app2, &tool, 1, 1, "done"),
            Step::Skipped { tool, reason } => emit_progress(&app2, &tool, 0, 0, &reason),
        })
    })
    .await
    .map_err(ui_err)?
    .map_err(ui_err)?;

    let mut cfg = state.config.lock().map_err(ui_err)?.clone();
    cfg.kiwix_exe = installed.kiwix_serve.clone();
    cfg.llama_exe = installed.llama_server.clone();
    cfg.pdfium_dir = Some(installed.pdfium_dir.clone());
    cfg.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = cfg;

    Ok(format!(
        "kiwix-serve: {}  llama-server: {}",
        installed.kiwix_serve.map_or("missing".into(), |p| p.display().to_string()),
        installed.llama_server.map_or("missing".into(), |p| p.display().to_string()),
    ))
}

/// Download a model by catalogue id into the models directory and point the
/// config at it, as chat model or reranker according to its role.
#[tauri::command]
async fn fetch_model(app: tauri::AppHandle, state: State<'_, AppState>, id: String, dest: String) -> UiResult<String> {
    use museion_core::{catalog, download::{self, Spec}};
    let model = catalog::models()
        .map_err(ui_err)?
        .into_iter()
        .find(|m| m.id == id)
        .ok_or_else(|| format!("no model {id:?} in the catalogue"))?;
    let dir = PathBuf::from(dest);
    let target = dir.join(&model.filename);
    let app2 = app.clone();
    let m2 = model.clone();
    let t2 = target.clone();
    tauri::async_runtime::spawn_blocking(move || -> anyhow::Result<()> {
        if download::is_verified(&t2, m2.size_bytes, &m2.sha256)? {
            return Ok(());
        }
        download::download_verified(
            &Spec { urls: &m2.urls, dest: &t2, size_bytes: m2.size_bytes, sha256: &m2.sha256 },
            |p| emit_progress(&app2, &m2.id, p.received, p.total, if p.resuming { "resuming" } else { "" }),
        )?;
        Ok(())
    })
    .await
    .map_err(ui_err)?
    .map_err(ui_err)?;

    let mut cfg = state.config.lock().map_err(ui_err)?.clone();
    cfg.models_dir = Some(dir);
    if model.role == "reranker" {
        cfg.reranker_model = Some(target.clone());
    } else {
        cfg.chat_model = Some(target.clone());
        cfg.chat_args = model.server_args.clone();
    }
    cfg.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = cfg;
    Ok(target.display().to_string())
}

/// Download every archive in a bundle into the library directory.
#[tauri::command]
async fn fetch_bundle(app: tauri::AppHandle, state: State<'_, AppState>, id: String) -> UiResult<String> {
    use museion_core::{catalog, download::{self, Spec}};
    let library = state
        .config
        .lock()
        .map_err(ui_err)?
        .library_dir
        .clone()
        .ok_or("choose a library folder first")?;
    let (_, sources) = catalog::bundle(&id).map_err(ui_err)?;
    let app2 = app.clone();
    let lib2 = library.clone();
    let n = tauri::async_runtime::spawn_blocking(move || -> anyhow::Result<usize> {
        let mut fetched = 0;
        for s in sources {
            let target = lib2.join(&s.filename);
            if download::is_verified(&target, s.size_bytes, &s.sha256)? {
                emit_progress(&app2, &s.id, s.size_bytes, s.size_bytes, "already present");
                continue;
            }
            let urls = s.all_urls();
            let result = download::download_verified(
                &Spec { urls: &urls, dest: &target, size_bytes: s.size_bytes, sha256: &s.sha256 },
                |p| emit_progress(&app2, &s.id, p.received, p.total, if p.resuming { "resuming" } else { "" }),
            );
            if let Err(e) = result {
                // All web sources gone. Hand over the torrent for the person's
                // own client rather than joining a swarm on their behalf.
                let mut msg = format!("{e:#}");
                if s.torrent.is_some() || s.magnet.is_some() {
                    msg.push_str("\n\nEvery web source failed. This archive is also on BitTorrent:");
                    if let Some(t) = &s.torrent {
                        msg.push_str(&format!("\n  torrent: {t}"));
                    }
                    if let Some(m) = s.short_magnet() {
                        msg.push_str(&format!("\n  magnet:  {m}"));
                    }
                    msg.push_str(&format!(
                        "\nDownload it with your own client, put {} in {}, and press Fetch again -- it will be verified and skipped.",
                        s.filename,
                        lib2.display()
                    ));
                }
                anyhow::bail!(msg);
            }
            fetched += 1;
        }
        Ok(fetched)
    })
    .await
    .map_err(ui_err)?
    .map_err(ui_err)?;
    Ok(format!("{n} archive(s) downloaded into {}", library.display()))
}

// ------------------------------------------------------------- catalogue

#[derive(Serialize)]
pub struct ContentItem {
    id: String,
    title: String,
    description: String,
    category: String,
    size_bytes: u64,
    /// Present in the library at the right size. A full hash is deferred to
    /// verify; this is a fast answer to "do I have it".
    installed: bool,
    /// Which bundles include it, for context.
    bundles: Vec<String>,
    /// The kiwix book id (archive filename without .zim), for opening it.
    filename: String,
}

#[derive(Serialize)]
pub struct Catalogue {
    items: Vec<ContentItem>,
    library_dir: Option<String>,
}

/// Everything that can be fetched, with what is already here.
#[tauri::command]
async fn list_content(state: State<'_, AppState>) -> UiResult<Catalogue> {
    use museion_core::catalog;
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let bundles = catalog::bundles().map_err(ui_err)?;
    let mut items = Vec::new();
    for s in catalog::sources().map_err(ui_err)? {
        let installed = cfg
            .library_dir
            .as_ref()
            .and_then(|d| std::fs::metadata(d.join(&s.filename)).ok())
            .is_some_and(|m| m.len() == s.size_bytes);
        let in_bundles = bundles
            .iter()
            .filter(|b| b.sources.iter().any(|x| x == &s.id))
            .map(|b| b.title.clone())
            .collect();
        items.push(ContentItem {
            id: s.id.clone(),
            title: s.title.clone(),
            description: s.description.clone(),
            category: s.category.clone(),
            size_bytes: s.size_bytes,
            installed,
            bundles: in_bundles,
            filename: s.filename.trim_end_matches(".zim").to_string(),
        });
    }
    // Survival first, then medicine, making, reference; smallest first within.
    let rank = |c: &str| match c {
        "survival" => 0,
        "medicine" => 1,
        "making" => 2,
        _ => 3,
    };
    items.sort_by(|a, b| rank(&a.category).cmp(&rank(&b.category)).then(a.size_bytes.cmp(&b.size_bytes)));
    Ok(Catalogue {
        items,
        library_dir: cfg.library_dir.as_ref().map(|p| p.display().to_string()),
    })
}

/// Fetch one archive, then make it askable: the content server is restarted
/// so it serves the new file, and the index is brought up to date. Progress
/// goes out on the same events the wizard uses.
#[tauri::command]
async fn fetch_source(app: tauri::AppHandle, state: State<'_, AppState>, id: String) -> UiResult<String> {
    use museion_core::{catalog, download::{self, Spec}};
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let library = cfg.library_dir.clone().ok_or("choose a library folder first")?;
    let s = catalog::sources()
        .map_err(ui_err)?
        .into_iter()
        .find(|s| s.id == id)
        .ok_or_else(|| format!("no archive {id:?} in the catalogue"))?;

    let target = library.join(&s.filename);
    let app2 = app.clone();
    let s2 = s.clone();
    let t2 = target.clone();
    let lib2 = library.clone();
    tauri::async_runtime::spawn_blocking(move || -> anyhow::Result<()> {
        if download::is_verified(&t2, s2.size_bytes, &s2.sha256)? {
            emit_progress(&app2, &s2.id, s2.size_bytes, s2.size_bytes, "already present");
            return Ok(());
        }
        let urls = s2.all_urls();
        let r = download::download_verified(
            &Spec { urls: &urls, dest: &t2, size_bytes: s2.size_bytes, sha256: &s2.sha256 },
            |p| emit_progress(&app2, &s2.id, p.received, p.total, if p.resuming { "resuming" } else { "" }),
        );
        if let Err(e) = r {
            let mut msg = format!("{e:#}");
            if s2.torrent.is_some() || s2.magnet.is_some() {
                msg.push_str("\n\nEvery web source failed. This archive is also on BitTorrent:");
                if let Some(t) = &s2.torrent {
                    msg.push_str(&format!("\n  torrent: {t}"));
                }
                if let Some(m) = s2.short_magnet() {
                    msg.push_str(&format!("\n  magnet:  {m}"));
                }
                msg.push_str(&format!(
                    "\nDownload it with your own client, put {} in {}, and press Fetch again.",
                    s2.filename,
                    lib2.display()
                ));
            }
            anyhow::bail!(msg);
        }
        Ok(())
    })
    .await
    .map_err(ui_err)?
    .map_err(ui_err)?;

    // Make it askable. kiwix-serve only knows the files it was started with.
    let mut note = String::new();
    if state.supervisor.is_running(Role::Kiwix) {
        if let Some(exe) = &cfg.kiwix_exe {
            emit_progress(&app, &s.id, 1, 1, "restarting the content server");
            state.supervisor.stop(Role::Kiwix);
            state
                .supervisor
                .start_kiwix(exe, &cfg.content_dirs(), cfg.kiwix_port)
                .map_err(ui_err)?;
            state
                .supervisor
                .wait_ready(
                    Role::Kiwix,
                    &format!("http://127.0.0.1:{}/catalog/v2/entries", cfg.kiwix_port),
                    Duration::from_secs(60),
                )
                .map_err(ui_err)?;
            emit_progress(&app, &s.id, 1, 1, "indexing");
            let summary = build_index(app.clone(), state.clone(), false).await?;
            note = format!(" Indexed: {summary}");
        }
    } else {
        note = " Start services to browse and ask it.".into();
    }
    Ok(format!("{} is in your library.{note}", s.title))
}

// ------------------------------------------------------- content folders

/// Add a folder of found content. Its archives are served and its PDFs are
/// indexed, right away if the services are up.
#[tauri::command]
async fn add_content_dir(app: tauri::AppHandle, state: State<'_, AppState>, path: String) -> UiResult<String> {
    let dir = PathBuf::from(&path);
    if !dir.is_dir() {
        return Err(format!("{path} is not a folder"));
    }
    let mut cfg = state.config.lock().map_err(ui_err)?.clone();
    if cfg.library_dir.as_ref() == Some(&dir) || cfg.extra_dirs.contains(&dir) {
        return Err("that folder is already part of the library".into());
    }
    cfg.extra_dirs.push(dir);
    cfg.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = cfg.clone();

    if !state.supervisor.is_running(Role::Kiwix) {
        return Ok("Folder added. Start services to serve and index it.".into());
    }
    let exe = cfg.kiwix_exe.clone().ok_or("kiwix-serve path not configured")?;
    state.supervisor.stop(Role::Kiwix);
    state
        .supervisor
        .start_kiwix(&exe, &cfg.content_dirs(), cfg.kiwix_port)
        .map_err(ui_err)?;
    state
        .supervisor
        .wait_ready(
            Role::Kiwix,
            &format!("http://127.0.0.1:{}/catalog/v2/entries", cfg.kiwix_port),
            Duration::from_secs(60),
        )
        .map_err(ui_err)?;
    let summary = build_index(app, state, false).await?;
    Ok(format!("Folder added and indexed. {summary}"))
}

/// Remove a folder. Its content leaves the index and the content server.
#[tauri::command]
async fn remove_content_dir(state: State<'_, AppState>, path: String) -> UiResult<String> {
    let dir = PathBuf::from(&path);
    let mut cfg = state.config.lock().map_err(ui_err)?.clone();
    cfg.extra_dirs.retain(|d| d != &dir);
    cfg.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = cfg.clone();

    // Forget what came from it: the folder's loose PDFs, and any archives
    // that lived there.
    if let Some(db) = cfg.index_path() {
        if let Ok(mut index) = Index::open(&db) {
            let _ = index.forget_book(&museion_core::indexer::folder_book_id(&dir));
            if let Ok(rd) = std::fs::read_dir(&dir) {
                for e in rd.flatten() {
                    if let Some(b) = e.file_name().to_string_lossy().strip_suffix(".zim") {
                        let _ = index.forget_book(b);
                    }
                }
            }
        }
    }
    state.forget_stats();

    if state.supervisor.is_running(Role::Kiwix) {
        if let Some(exe) = &cfg.kiwix_exe {
            state.supervisor.stop(Role::Kiwix);
            state
                .supervisor
                .start_kiwix(exe, &cfg.content_dirs(), cfg.kiwix_port)
                .map_err(ui_err)?;
        }
    }
    Ok("Folder removed.".into())
}

/// Open a file with whatever the system uses for it. For PDFs found in a
/// folder, which the content server cannot serve.
#[tauri::command]
fn open_path(app: tauri::AppHandle, path: String) -> UiResult<()> {
    use tauri_plugin_shell::ShellExt;
    // The shell plugin's open() is deprecated in favour of a separate opener
    // plugin. It still works, and it is already bundled and permitted; one
    // call does not justify another plugin, another permission, and another
    // dependency to pin.
    #[allow(deprecated)]
    app.shell().open(path, None).map_err(ui_err)
}

#[tauri::command]
fn kiwix_url(state: State<AppState>) -> UiResult<String> {
    let cfg = state.config.lock().map_err(ui_err)?;
    Ok(format!("http://127.0.0.1:{}", cfg.kiwix_port))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let dir = app.path().app_config_dir()?;
            let config_path = dir.join("config.json");
            let config = Config::load(&config_path);
            app.manage(AppState {
                config: Mutex::new(config),
                config_path,
                supervisor: Supervisor::new(),
                hardware: std::sync::OnceLock::new(),
                stats: Mutex::new(None),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_hardware,
            get_status,
            service_state,
            get_config,
            set_config,
            start_services,
            stop_services,
            ask,
            build_index,
            fit_chat_model,
            setup_plan,
            fetch_tools,
            fetch_model,
            fetch_bundle,
            list_content,
            fetch_source,
            add_content_dir,
            remove_content_dir,
            open_path,
            kiwix_url,
        ])
        .build(tauri::generate_context!())
        .expect("failed to start Museion")
        .run(|app, event| {
            // Both hooks matter. Closing the last window does not necessarily
            // end the process, and a chat model left resident holds the whole
            // GPU until someone kills it by hand.
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<AppState>() {
                    state.supervisor.stop_all();
                }
            }
        });
}
