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
}

impl Config {
    pub fn with_defaults() -> Self {
        Self {
            kiwix_port: 8090,
            chat_port: 8080,
            reranker_port: 8081,
            chat_gpu_layers: 999,
            ..Default::default()
        }
    }

    pub fn load(path: &Path) -> Self {
        std::fs::read_to_string(path)
            .ok()
            .and_then(|s| serde_json::from_str::<Config>(&s).ok())
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

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating {}", parent.display()))?;
        }
        std::fs::write(path, serde_json::to_string_pretty(self)?)
            .with_context(|| format!("writing {}", path.display()))?;
        Ok(())
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
