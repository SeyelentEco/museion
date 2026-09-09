//! Running from a drive.
//!
//! A file named `museion-portable` beside the executable makes its folder the
//! portable root: config, library, models and helper programs are found
//! relative to it, so the drive can come up as E: on one computer and H: on
//! the next and nothing is lost. What belongs to the *computer* rather than
//! the drive -- which llama.cpp build to run, which model tier fits, the GPU
//! split the fit measured -- lives in a machine profile under the user's
//! local app data, keyed by the id in the marker, and is laid over the drive's
//! config at launch. The drive itself is never written for that.

use crate::config::Config;
use museion_core::hardware::{Accelerator, Tier};
use museion_core::{catalog, Hardware};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const MARKER: &str = "museion-portable";

pub struct Portable {
    pub root: PathBuf,
    /// From the marker; keys the per-machine state. Kept for the person
    /// reading a profile folder, which is named after it.
    #[allow(dead_code)]
    pub id: String,
    /// Per-machine, per-drive state: the profile, and anything the app writes
    /// for itself when the drive is read-only.
    pub cache_dir: PathBuf,
}

impl Portable {
    /// Portable if the marker sits beside the running executable.
    pub fn detect(local_data_dir: &Path) -> Option<Portable> {
        let exe = std::env::current_exe().ok()?;
        let root = exe.parent()?.to_path_buf();
        let marker = root.join(MARKER);
        if !marker.exists() {
            return None;
        }
        let raw = std::fs::read_to_string(&marker).unwrap_or_default();
        let id: String = raw
            .trim()
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
            .take(64)
            .collect();
        let id = if id.is_empty() { "drive".to_string() } else { id };
        let cache_dir = local_data_dir.join("portable").join(&id);
        let _ = std::fs::create_dir_all(&cache_dir);
        Some(Portable { root, id, cache_dir })
    }

    pub fn config_path(&self) -> PathBuf {
        self.root.join("config.json")
    }

    pub fn tuning_path(&self) -> PathBuf {
        self.root.join("tuning.yaml")
    }

    pub fn profile_path(&self) -> PathBuf {
        self.cache_dir.join("machine.json")
    }

    /// `wanted` if it can be created and written to, else a folder of the
    /// same name in the cache. A write-protected drive still answers
    /// questions; what the app writes for itself goes somewhere it can.
    pub fn writable_or_cache(&self, wanted: &Path) -> PathBuf {
        if std::fs::create_dir_all(wanted).is_ok() && is_writable(wanted) {
            return wanted.to_path_buf();
        }
        let name = wanted.file_name().map(|n| n.to_os_string()).unwrap_or_else(|| "data".into());
        let alt = self.cache_dir.join(name);
        let _ = std::fs::create_dir_all(&alt);
        alt
    }
}

fn is_writable(dir: &Path) -> bool {
    let probe = dir.join(".museion-write-test");
    match std::fs::write(&probe, b"") {
        Ok(()) => {
            let _ = std::fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

/// What this computer runs from the drive, and how.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct MachineProfile {
    /// For a person reading the file: which computer this was measured on.
    pub machine: String,
    pub llama_exe: Option<PathBuf>,
    pub chat_model: Option<PathBuf>,
    pub chat_args: Vec<String>,
    pub chat_gpu_layers: u32,
    /// Whether llama-fit-params has measured the split on this computer.
    pub fitted: bool,
}

impl MachineProfile {
    pub fn load(path: &Path) -> Option<Self> {
        let s = std::fs::read_to_string(path).ok()?;
        serde_json::from_str(s.trim_start_matches('\u{feff}')).ok()
    }

    pub fn save(&self, path: &Path) -> anyhow::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(path, serde_json::to_string_pretty(self)?)?;
        Ok(())
    }

    /// Take the machine-specific fields from a config.
    pub fn absorb(&mut self, cfg: &Config) {
        self.llama_exe = cfg.llama_exe.clone();
        self.chat_model = cfg.chat_model.clone();
        self.chat_args = cfg.chat_args.clone();
        self.chat_gpu_layers = cfg.chat_gpu_layers;
    }

    /// Lay the machine-specific fields over a config.
    pub fn apply(&self, cfg: &mut Config) {
        if self.llama_exe.as_ref().is_some_and(|p| p.exists()) {
            cfg.llama_exe = self.llama_exe.clone();
        }
        if self.chat_model.as_ref().is_some_and(|p| p.exists()) {
            cfg.chat_model = self.chat_model.clone();
            cfg.chat_args = self.chat_args.clone();
            cfg.chat_gpu_layers = self.chat_gpu_layers;
        }
    }
}

/// The first launch on a computer: fill in what the drive carries and choose
/// the build and the model for this hardware. Returns the profile to save;
/// `fitted` is false when a GPU is present and the split still needs measuring.
pub fn adapt(cfg: &mut Config, hw: &Hardware, root: &Path) -> MachineProfile {
    let bin = root.join("bin");
    let missing = |p: &Option<PathBuf>| p.as_ref().map_or(true, |p| !p.exists());

    if missing(&cfg.library_dir) && root.join("library").is_dir() {
        cfg.library_dir = Some(root.join("library"));
    }
    if missing(&cfg.models_dir) && root.join("models").is_dir() {
        cfg.models_dir = Some(root.join("models"));
    }
    if missing(&cfg.kiwix_exe) {
        cfg.kiwix_exe = find_exe(&bin, "kiwix-serve");
    }
    if missing(&cfg.pdfium_dir) && bin.is_dir() {
        cfg.pdfium_dir = Some(bin.clone());
    }

    // The llama.cpp build: the drive may carry one per accelerator under
    // bin\llama\<name>, or a single flattened build in bin.
    let order: &[&str] = match hw.accelerator {
        Accelerator::Cuda { .. } => &["cuda", "vulkan", "cpu"],
        Accelerator::Vulkan { .. } => &["vulkan", "cpu"],
        Accelerator::Metal { .. } => &["metal", "cpu"],
        Accelerator::Cpu => &["cpu", "vulkan"],
    };
    let llama = order
        .iter()
        .find_map(|v| find_exe(&bin.join("llama").join(v), "llama-server"))
        .or_else(|| find_exe(&bin, "llama-server"));
    if llama.is_some() {
        cfg.llama_exe = llama;
    }

    // The model: the catalogue's pick for this tier if the drive has it,
    // else the largest model on the drive that this tier may run, else
    // whatever the drive's config named if that exists.
    let models_dir = cfg.models_dir.clone().unwrap_or_else(|| root.join("models"));
    let present = |m: &catalog::Model| models_dir.join(&m.filename).exists();
    let reranker = catalog::reranker_model().ok().flatten();
    let is_reranker = |m: &catalog::Model| reranker.as_ref().is_some_and(|r| r.id == m.id);
    let all = catalog::models().unwrap_or_default();
    let pick = catalog::chat_model_for(hw.tier, hw.system_ram_mb)
        .ok()
        .flatten()
        .filter(|m| present(m))
        .or_else(|| {
            all.iter()
                .filter(|m| present(m) && !is_reranker(m))
                .filter(|m| m.tiers.iter().filter_map(|t| tier_named(t)).any(|t| t <= hw.tier))
                .max_by_key(|m| m.size_bytes)
                .cloned()
        });
    if let Some(m) = pick {
        cfg.chat_model = Some(models_dir.join(&m.filename));
        cfg.chat_args = m.server_args.clone();
    }
    if let Some(r) = &reranker {
        let p = models_dir.join(&r.filename);
        if p.exists() {
            cfg.reranker_model = Some(p);
        }
    }
    let cpu_only = matches!(hw.accelerator, Accelerator::Cpu);
    cfg.chat_gpu_layers = if cpu_only { 0 } else { 999 };

    let mut profile = MachineProfile {
        machine: describe(hw),
        // Nothing to fit without a GPU: everything runs on the CPU.
        fitted: cpu_only,
        ..Default::default()
    };
    profile.absorb(cfg);
    profile
}

fn tier_named(s: &str) -> Option<Tier> {
    match s.trim().to_ascii_lowercase().as_str() {
        "modest" => Some(Tier::Modest),
        "capable" => Some(Tier::Capable),
        "generous" => Some(Tier::Generous),
        "ample" => Some(Tier::Ample),
        _ => None,
    }
}

fn find_exe(dir: &Path, stem: &str) -> Option<PathBuf> {
    let name = if cfg!(windows) { format!("{stem}.exe") } else { stem.to_string() };
    let p = dir.join(name);
    p.exists().then_some(p)
}

fn describe(hw: &Hardware) -> String {
    let host = std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "this computer".into());
    let gpu = match &hw.accelerator {
        Accelerator::Cuda { name, vram_mb } => format!("{name} ({vram_mb} MB, CUDA)"),
        Accelerator::Vulkan { name, vram_mb } => format!("{name} ({vram_mb} MB, Vulkan)"),
        Accelerator::Metal { name, unified_mb } => format!("{name} ({unified_mb} MB unified, Metal)"),
        Accelerator::Cpu => "no GPU".into(),
    };
    format!("{host}: {gpu}, {} MB RAM, tier {:?}", hw.system_ram_mb, hw.tier)
}
