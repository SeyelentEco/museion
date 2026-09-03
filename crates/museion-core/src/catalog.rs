//! The content, model and tool catalogue, embedded in the binary.
//!
//! The YAML under catalog/, models/ and tools/ is compiled in. An installed app
//! has no repository checkout to read from, and a tool built for the moment
//! the network is gone must carry its own knowledge of where everything came
//! from -- the checksums above all, since those are what let a library on a
//! drive in a cupboard prove it is still intact years later.

use crate::hardware::{Accelerator, Tier};
use anyhow::{anyhow, Context, Result};
use include_dir::{include_dir, Dir};
use serde::Deserialize;

static CATALOG: Dir = include_dir!("$CARGO_MANIFEST_DIR/../../catalog");
static MODELS: Dir = include_dir!("$CARGO_MANIFEST_DIR/../../models");
static TOOLS: Dir = include_dir!("$CARGO_MANIFEST_DIR/../../tools");

#[derive(Debug, Clone, Deserialize)]
pub struct License {
    pub name: String,
    #[serde(default)]
    pub attribution: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Source {
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub description: String,
    pub category: String,
    pub filename: String,
    pub size_bytes: u64,
    pub sha256: String,
    pub urls: Vec<String>,
    #[serde(default)]
    pub mirrors: Vec<String>,
    pub license: Option<License>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Bundle {
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub description: String,
    pub sources: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Requires {
    #[serde(default)]
    pub system_ram_mb: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Model {
    pub id: String,
    pub title: String,
    pub role: String,
    #[serde(default)]
    pub tiers: Vec<String>,
    pub filename: String,
    pub size_bytes: u64,
    pub sha256: String,
    pub urls: Vec<String>,
    #[serde(default)]
    pub server_args: Vec<String>,
    pub requires: Option<Requires>,
    pub license: Option<License>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ToolVariant {
    #[serde(default)]
    pub accelerator: Option<String>,
    #[serde(default)]
    pub platform: Option<String>,
    #[serde(default)]
    pub arch: Option<String>,
    #[serde(default)]
    pub internal: bool,
    pub file: String,
    pub url: String,
    pub size_bytes: u64,
    pub sha256: String,
    #[serde(default)]
    pub also: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Tool {
    pub id: String,
    pub name: String,
    pub provides: Vec<String>,
    pub variants: std::collections::BTreeMap<String, ToolVariant>,
}

impl Source {
    /// Every URL to try, primary first.
    pub fn all_urls(&self) -> Vec<String> {
        self.urls.iter().chain(self.mirrors.iter()).cloned().collect()
    }
}

fn parse_dir<T: for<'de> Deserialize<'de>>(dir: &'static Dir, sub: Option<&str>) -> Result<Vec<T>> {
    let d = match sub {
        Some(s) => dir.get_dir(s).ok_or_else(|| anyhow!("embedded directory {s} missing"))?,
        None => dir,
    };
    let mut out = Vec::new();
    for f in d.files() {
        let name = f.path().to_string_lossy();
        if !(name.ends_with(".yaml") || name.ends_with(".yml")) {
            continue;
        }
        let text = f.contents_utf8().ok_or_else(|| anyhow!("{name} is not UTF-8"))?;
        out.push(serde_yaml::from_str::<T>(text).with_context(|| format!("parsing {name}"))?);
    }
    Ok(out)
}

pub fn sources() -> Result<Vec<Source>> {
    parse_dir(&CATALOG, Some("sources"))
}

pub fn bundles() -> Result<Vec<Bundle>> {
    parse_dir(&CATALOG, Some("bundles"))
}

pub fn models() -> Result<Vec<Model>> {
    parse_dir(&MODELS, None)
}

pub fn tools() -> Result<Vec<Tool>> {
    parse_dir(&TOOLS, None)
}

pub fn bundle(id: &str) -> Result<(Bundle, Vec<Source>)> {
    let b = bundles()?
        .into_iter()
        .find(|b| b.id == id)
        .ok_or_else(|| anyhow!("no bundle named {id:?}"))?;
    let all = sources()?;
    let mut picked = Vec::new();
    for sid in &b.sources {
        picked.push(
            all.iter()
                .find(|s| &s.id == sid)
                .cloned()
                .ok_or_else(|| anyhow!("bundle {id} references unknown source {sid:?}"))?,
        );
    }
    Ok((b, picked))
}

/// The chat model recommended for a tier, honouring its RAM requirement.
///
/// If the tier's model needs more system RAM than the machine has, the next
/// tier down is offered rather than a model that will fail to load.
pub fn chat_model_for(tier: Tier, system_ram_mb: u64) -> Result<Option<Model>> {
    let all = models()?;
    let mut t = tier;
    loop {
        let name = format!("{t:?}");
        if let Some(m) = all.iter().find(|m| {
            m.role == "chat"
                && m.tiers.iter().any(|x| x == &name)
                && m.requires.as_ref().map_or(true, |r| r.system_ram_mb <= system_ram_mb)
        }) {
            return Ok(Some(m.clone()));
        }
        t = match t {
            Tier::Ample => Tier::Generous,
            Tier::Generous => Tier::Capable,
            Tier::Capable => Tier::Modest,
            Tier::Modest => return Ok(None),
        };
    }
}

pub fn reranker_model() -> Result<Option<Model>> {
    Ok(models()?.into_iter().find(|m| m.role == "reranker"))
}

/// Which tool variant suits this machine.
pub fn tool_variant<'a>(tool: &'a Tool, accel: &Accelerator) -> Option<(&'a str, &'a ToolVariant)> {
    let platform = if cfg!(windows) { "windows" } else if cfg!(target_os = "macos") { "darwin" } else { "linux" };
    let arch = if cfg!(target_arch = "aarch64") { "arm64" } else { "x64" };
    let want = match accel {
        Accelerator::Cuda { .. } => "cuda",
        Accelerator::Metal { .. } => "metal",
        Accelerator::Vulkan { .. } => "vulkan",
        Accelerator::Cpu => "cpu",
    };
    let fits = |v: &ToolVariant| {
        !v.internal
            && v.platform.as_deref().map_or(true, |p| p == platform)
            && v.arch.as_deref().map_or(true, |a| a == arch)
    };
    let pick = |pred: &dyn Fn(&ToolVariant) -> bool| {
        tool.variants.iter().find(|(_, v)| fits(v) && pred(v)).map(|(k, v)| (k.as_str(), v))
    };
    pick(&|v| v.accelerator.as_deref() == Some(want))
        .or_else(|| pick(&|v| v.accelerator.is_none()))
        .or_else(|| pick(&|v| v.accelerator.as_deref() == Some("cpu")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_catalogue_parses_and_bundles_resolve() {
        assert!(!sources().unwrap().is_empty());
        let (b, srcs) = bundle("go-bag").unwrap();
        assert_eq!(b.sources.len(), srcs.len());
        assert!(srcs.iter().all(|s| s.sha256.len() == 64));
    }

    #[test]
    fn a_tier_short_of_ram_falls_back_rather_than_failing() {
        // Capable wants 24 GB of system RAM for expert offload.
        let m = chat_model_for(Tier::Capable, 8_000).unwrap().unwrap();
        assert!(m.tiers.contains(&"Modest".to_string()));
        let m = chat_model_for(Tier::Capable, 64_000).unwrap().unwrap();
        assert!(m.tiers.contains(&"Capable".to_string()));
    }

    #[test]
    fn cuda_machine_gets_the_cuda_llama_build_with_its_companion() {
        let t = tools().unwrap().into_iter().find(|t| t.id == "llama-cpp").unwrap();
        let accel = Accelerator::Cuda { name: "x".into(), vram_mb: 12_000 };
        if cfg!(windows) {
            let (k, v) = tool_variant(&t, &accel).unwrap();
            assert_eq!(k, "windows-cuda");
            assert_eq!(v.also, vec!["windows-cudart".to_string()]);
        }
    }
}
