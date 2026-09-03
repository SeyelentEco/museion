//! Installing the helper programs: kiwix-serve, llama-server, pdfium.
//!
//! The Rust twin of scripts/fetch-tools.mjs, so the installed app can do it
//! without a repository checkout. Same choices, for the same reasons:
//! archives are unpacked with whatever the system already has (bsdtar handles
//! both zip and tar.gz and ships with Windows 10+ and macOS; GNU tar reads a
//! Windows drive letter as a remote host, hence --force-local), and every
//! publisher's layout is flattened into one directory afterwards.

use crate::catalog::{self, ToolVariant};
use crate::download::{self, Spec};
use crate::hardware::Accelerator;
use anyhow::{anyhow, bail, Context, Result};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone)]
pub enum Step {
    Downloading { tool: String, file: String, received: u64, total: u64 },
    Unpacking { tool: String },
    Done { tool: String },
    Skipped { tool: String, reason: String },
}

/// Where the installed binaries ended up.
#[derive(Debug, Clone)]
pub struct Installed {
    pub kiwix_serve: Option<PathBuf>,
    pub llama_server: Option<PathBuf>,
    pub pdfium_dir: PathBuf,
}

/// Which archives this machine would fetch, with sizes, for showing a plan.
pub fn plan(accel: &Accelerator) -> Result<Vec<(String, String, u64)>> {
    let mut out = Vec::new();
    for t in catalog::tools()? {
        if let Some((key, v)) = catalog::tool_variant(&t, accel) {
            let mut size = v.size_bytes;
            for k in &v.also {
                if let Some(c) = t.variants.get(k) {
                    size += c.size_bytes;
                }
            }
            out.push((t.name.clone(), key.to_string(), size));
        }
    }
    Ok(out)
}

pub fn install(accel: &Accelerator, dest: &Path, mut report: impl FnMut(Step)) -> Result<Installed> {
    fs::create_dir_all(dest)?;
    let staging = std::env::temp_dir().join(format!("museion-tools-{}", std::process::id()));
    fs::create_dir_all(&staging)?;

    let result = (|| -> Result<()> {
        for tool in catalog::tools()? {
            let Some((_, variant)) = catalog::tool_variant(&tool, accel) else {
                report(Step::Skipped {
                    tool: tool.name.clone(),
                    reason: "no build for this platform".into(),
                });
                continue;
            };
            let mut parts: Vec<&ToolVariant> = vec![variant];
            for k in &variant.also {
                parts.push(tool.variants.get(k).ok_or_else(|| anyhow!("{} names missing companion {k}", tool.id))?);
            }
            for part in parts {
                let archive = staging.join(&part.file);
                if !download::is_verified(&archive, part.size_bytes, &part.sha256)? {
                    let urls = vec![part.url.clone()];
                    let tool_name = tool.name.clone();
                    let file = part.file.clone();
                    download::download_verified(
                        &Spec { urls: &urls, dest: &archive, size_bytes: part.size_bytes, sha256: &part.sha256 },
                        |p| report(Step::Downloading {
                            tool: tool_name.clone(),
                            file: file.clone(),
                            received: p.received,
                            total: p.total,
                        }),
                    )?;
                }
                report(Step::Unpacking { tool: tool.name.clone() });
                extract(&archive, dest)?;
            }
            report(Step::Done { tool: tool.name.clone() });
        }
        flatten(dest)?;
        Ok(())
    })();

    let _ = fs::remove_dir_all(&staging);
    result?;

    Ok(Installed {
        kiwix_serve: find(dest, "kiwix-serve"),
        llama_server: find(dest, "llama-server"),
        pdfium_dir: dest.to_path_buf(),
    })
}

fn find(dir: &Path, stem: &str) -> Option<PathBuf> {
    let exe = if cfg!(windows) { format!("{stem}.exe") } else { stem.to_string() };
    let p = dir.join(exe);
    p.exists().then_some(p)
}

fn extract(archive: &Path, into: &Path) -> Result<()> {
    let is_zip = archive.extension().is_some_and(|e| e.eq_ignore_ascii_case("zip"));
    let a = archive.to_string_lossy().to_string();
    let i = into.to_string_lossy().to_string();
    let mut candidates: Vec<(&str, Vec<String>)> = vec![
        (r"C:\Windows\System32\tar.exe", vec!["-xf".into(), a.clone(), "-C".into(), i.clone()]),
        ("tar", vec!["-xf".into(), "--force-local".into(), a.clone(), "-C".into(), i.clone()]),
        ("tar", vec![if is_zip { "-xf" } else { "-xzf" }.into(), a.clone(), "-C".into(), i.clone()]),
    ];
    if is_zip {
        candidates.push(("unzip", vec!["-oq".into(), a.clone(), "-d".into(), i.clone()]));
    }
    let mut last = String::new();
    for (cmd, args) in candidates {
        match Command::new(cmd).args(&args).output() {
            Ok(o) if o.status.success() => return Ok(()),
            Ok(o) => last = String::from_utf8_lossy(&o.stderr).trim().to_string(),
            Err(e) => last = e.to_string(),
        }
    }
    bail!("could not unpack {}: {last}", archive.display())
}

/// Move every executable and shared library up into `dir`.
fn flatten(dir: &Path) -> Result<()> {
    fn walk(root: &Path, d: &Path, depth: usize) -> Result<()> {
        if depth > 4 {
            return Ok(());
        }
        for entry in fs::read_dir(d)? {
            let p = entry?.path();
            if p.is_dir() {
                walk(root, &p, depth + 1)?;
            } else if d != root {
                let name = p.file_name().unwrap().to_string_lossy().to_string();
                let wanted = name.ends_with(".exe")
                    || name.ends_with(".dll")
                    || name.ends_with(".dylib")
                    || name.contains(".so")
                    || !name.contains('.');
                let target = root.join(&name);
                if wanted && !target.exists() {
                    fs::rename(&p, &target).with_context(|| format!("moving {name}"))?;
                }
            }
        }
        Ok(())
    }
    walk(dir, dir, 0)
}
