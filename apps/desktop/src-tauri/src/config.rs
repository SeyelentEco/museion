//! Where everything lives on this machine.
//!
//! There is deliberately no default library location. Content runs from under a
//! gigabyte to well over 250 GB, and a tool that silently fills someone's system
//! drive with Wikipedia has done something rude. The path is asked for once and
//! then remembered.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct Config {
    /// Directory holding .zim archives and the search index.
    pub library_dir: Option<PathBuf>,
    /// Other folders of content someone has found: .zim archives are served
    /// and indexed like catalogue ones, loose PDFs are indexed by page. The
    /// catalogue is a starting point, not a boundary.
    pub extra_dirs: Vec<PathBuf>,
    /// The kiwix-serve binary. A full path rather than a directory, because
    /// kiwix-tools and llama.cpp come from different upstreams and many people
    /// already have one or both installed somewhere of their own choosing.
    pub kiwix_exe: Option<PathBuf>,
    /// The llama-server binary.
    pub llama_exe: Option<PathBuf>,
    /// Directory holding the pdfium library, used when indexing PDF archives.
    pub pdfium_dir: Option<PathBuf>,
    /// Directory the setup flow downloads models into.
    pub models_dir: Option<PathBuf>,
    /// GGUF for the chat model.
    pub chat_model: Option<PathBuf>,
    /// GGUF for the reranker.
    pub reranker_model: Option<PathBuf>,
    /// Extra llama-server arguments for the chat model, normally produced by
    /// llama-fit-params against the actual card.
    pub chat_args: Vec<String>,
    /// How many layers of the chat model to put on the GPU.
    pub chat_gpu_layers: u32,
    pub kiwix_port: u16,
    pub chat_port: u16,
    pub reranker_port: u16,
    /// Start the services when the app opens. On by default: a person opening
    /// Museion wants to ask something. Off for those who would rather the GPU
    /// stay free until they choose -- the model holds nearly all of it.
    #[serde(default = "yes")]
    pub autostart: bool,
    /// Set when running from a drive: paths are stored relative to this
    /// folder and resolved against it, so the drive's letter can change.
    #[serde(skip)]
    pub base: Option<PathBuf>,
}

fn yes() -> bool {
    true
}

impl Config {
    pub fn with_defaults() -> Self {
        Self {
            kiwix_port: 8090,
            chat_port: 8080,
            reranker_port: 8081,
            chat_gpu_layers: 999,
            autostart: true,
            ..Default::default()
        }
    }

    pub fn load(path: &Path) -> Self {
        std::fs::read_to_string(path)
            .ok()
            // Notepad and PowerShell 5 write a UTF-8 byte-order mark, which
            // serde_json rejects. A config that fails to parse becomes an
            // empty one, and an empty one shows the first-run screen as if
            // the library had never existed; that is not a fair price for
            // editing the file by hand.
            .and_then(|s| serde_json::from_str::<Config>(s.trim_start_matches('\u{feff}')).ok())
            .map(|mut c| {
                // A config written by an older build may be missing ports.
                if c.kiwix_port == 0 {
                    c.kiwix_port = 8090;
                }
                if c.chat_port == 0 {
                    c.chat_port = 8080;
                }
                if c.reranker_port == 0 {
                    c.reranker_port = 8081;
                }
                c
            })
            .unwrap_or_else(Self::with_defaults)
    }

    /// Load, then resolve every path against `base`: a drive's config says
    /// `library`, and means the library folder beside it wherever it is.
    pub fn load_portable(path: &Path, base: &Path) -> Self {
        let mut c = Self::load(path);
        let resolve = |p: &mut Option<PathBuf>| {
            if let Some(v) = p.as_mut() {
                if v.is_relative() {
                    *v = base.join(&*v);
                }
            }
        };
        resolve(&mut c.library_dir);
        resolve(&mut c.kiwix_exe);
        resolve(&mut c.llama_exe);
        resolve(&mut c.pdfium_dir);
        resolve(&mut c.models_dir);
        resolve(&mut c.chat_model);
        resolve(&mut c.reranker_model);
        for d in c.extra_dirs.iter_mut() {
            if d.is_relative() {
                *d = base.join(&*d);
            }
        }
        c.base = Some(base.to_path_buf());
        c
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating {}", parent.display()))?;
        }
        // On a drive, anything under the root is written relative to it.
        let mut out = self.clone();
        if let Some(base) = &self.base {
            let rel = |p: &mut Option<PathBuf>| {
                if let Some(v) = p.as_mut() {
                    if let Ok(r) = v.strip_prefix(base) {
                        *v = r.to_path_buf();
                    }
                }
            };
            rel(&mut out.library_dir);
            rel(&mut out.kiwix_exe);
            rel(&mut out.llama_exe);
            rel(&mut out.pdfium_dir);
            rel(&mut out.models_dir);
            rel(&mut out.chat_model);
            rel(&mut out.reranker_model);
            for d in out.extra_dirs.iter_mut() {
                if let Ok(r) = d.strip_prefix(base) {
                    *d = r.to_path_buf();
                }
            }
        }
        std::fs::write(path, serde_json::to_string_pretty(&out)?)
            .with_context(|| format!("writing {}", path.display()))?;
        Ok(())
    }

    /// Every folder content is read from: the library first, then extras.
    pub fn content_dirs(&self) -> Vec<PathBuf> {
        let mut v: Vec<PathBuf> = self.library_dir.iter().cloned().collect();
        v.extend(self.extra_dirs.iter().cloned());
        v
    }

    pub fn index_path(&self) -> Option<PathBuf> {
        self.library_dir.as_ref().map(|d| d.join("museion-index.db"))
    }

    /// True once there is enough here to actually answer a question.
    pub fn is_ready(&self) -> bool {
        self.library_dir.as_ref().is_some_and(|d| d.exists())
            && self.chat_model.as_ref().is_some_and(|m| m.exists())
    }
}
