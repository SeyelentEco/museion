//! Resumable, verified downloads -- the Rust twin of scripts/lib/download.js.
//!
//! Same rules, for the same reasons:
//!
//!   * Bytes land in `<name>.part` and are renamed only after the hash matches,
//!     so a file under its real name is always a good file.
//!   * Resume re-hashes what is already on disk before appending, so the final
//!     digest covers the whole file without a second read.
//!   * A server that ignores a Range request and answers 200 would silently
//!     append a second copy of the file from byte zero; that is detected.
//!   * A bad hash deletes the partial file: the bytes are wrong, not merely
//!     incomplete, and keeping them poisons every future resume.

use anyhow::{bail, Context, Result};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub struct Progress {
    pub received: u64,
    pub total: u64,
    /// True while re-hashing an existing partial file rather than downloading.
    pub resuming: bool,
}

pub struct Spec<'a> {
    pub urls: &'a [String],
    pub dest: &'a Path,
    pub size_bytes: u64,
    pub sha256: &'a str,
}

/// Is the file already present and correct?
pub fn is_verified(path: &Path, size_bytes: u64, sha256: &str) -> Result<bool> {
    let Ok(meta) = fs::metadata(path) else {
        return Ok(false);
    };
    if meta.len() != size_bytes {
        return Ok(false);
    }
    Ok(hash_file(path, &mut Sha256::new(), |_| {})? == sha256)
}

fn hash_file(path: &Path, hasher: &mut Sha256, mut on: impl FnMut(u64)) -> Result<String> {
    let mut f = File::open(path)?;
    let mut buf = vec![0u8; 1 << 20];
    let mut read = 0u64;
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        read += n as u64;
        on(read);
    }
    Ok(format!("{:x}", hasher.clone().finalize()))
}

/// Download one file, trying each URL in turn, resuming a prior `.part`.
pub fn download_verified(spec: &Spec, mut on_progress: impl FnMut(Progress)) -> Result<String> {
    if let Some(parent) = spec.dest.parent() {
        fs::create_dir_all(parent)?;
    }
    let part = spec.dest.with_extension(format!(
        "{}.part",
        spec.dest.extension().and_then(|e| e.to_str()).unwrap_or("")
    ));

    let mut last: Option<anyhow::Error> = None;
    for url in spec.urls {
        match attempt(url, spec, &part, &mut on_progress) {
            Ok(()) => return Ok(url.clone()),
            Err(e) => last = Some(e),
        }
    }
    bail!(
        "all sources failed for {}\n  last error: {}",
        spec.dest.display(),
        last.map(|e| format!("{e:#}")).unwrap_or_default()
    )
}

fn attempt(
    url: &str,
    spec: &Spec,
    part: &Path,
    on_progress: &mut impl FnMut(Progress),
) -> Result<()> {
    let mut hasher = Sha256::new();
    let mut offset = 0u64;

    if let Ok(meta) = fs::metadata(part) {
        if meta.len() > 0 && meta.len() < spec.size_bytes {
            hash_file(part, &mut hasher, |read| {
                on_progress(Progress { received: read, total: meta.len(), resuming: true })
            })?;
            offset = meta.len();
        } else if meta.len() >= spec.size_bytes {
            // Oversized or complete-but-unrenamed: start clean rather than guess.
            fs::remove_file(part)?;
        }
    }

    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(20))
        .timeout_read(Duration::from_secs(120))
        .build();
    let mut req = agent.get(url);
    if offset > 0 {
        req = req.set("Range", &format!("bytes={offset}-"));
    }
    let res = req.call().with_context(|| format!("requesting {url}"))?;

    if offset > 0 && res.status() != 206 {
        let _ = fs::remove_file(part);
        bail!("{url} does not support resume (replied {}, expected 206)", res.status());
    }

    let mut out = OpenOptions::new()
        .create(true)
        .append(offset > 0)
        .write(true)
        .truncate(offset == 0)
        .open(part)?;

    let mut reader = res.into_reader();
    let mut buf = vec![0u8; 1 << 20];
    let mut received = offset;
    let mut last_tick = Instant::now();
    loop {
        let n = reader.read(&mut buf).with_context(|| format!("reading from {url}"))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        out.write_all(&buf[..n])?;
        received += n as u64;
        if last_tick.elapsed() > Duration::from_millis(250) {
            last_tick = Instant::now();
            on_progress(Progress { received, total: spec.size_bytes, resuming: false });
        }
    }
    out.flush()?;
    drop(out);
    on_progress(Progress { received, total: spec.size_bytes, resuming: false });

    let len = fs::metadata(part)?.len();
    if len != spec.size_bytes {
        bail!("size mismatch: expected {} bytes, got {len}", spec.size_bytes);
    }
    let digest = format!("{:x}", hasher.finalize());
    if digest != spec.sha256 {
        let _ = fs::remove_file(part);
        bail!("checksum mismatch\n    expected {}\n    got      {digest}", spec.sha256);
    }
    fs::rename(part, spec.dest)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verified_rejects_wrong_size_before_hashing() {
        let dir = std::env::temp_dir().join(format!("museion-dl-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let p = dir.join("x.bin");
        fs::write(&p, b"hello").unwrap();
        // Right hash for "hello" but wrong declared size: must be false.
        let h = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";
        assert!(!is_verified(&p, 99, h).unwrap());
        assert!(is_verified(&p, 5, h).unwrap());
        fs::remove_dir_all(&dir).ok();
    }
}

/// Download a file with no known hash or size -- the WebView2 runtime
/// installer, which Microsoft republishes under one link -- to a `.part`
/// renamed into place when complete. Nothing verified by a checksum should
/// go through here.
pub fn fetch_unverified(url: &str, dest: &Path, mut on_progress: impl FnMut(u64, u64)) -> Result<()> {
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent)?;
    }
    let part = dest.with_extension("part");
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(20))
        .timeout_read(Duration::from_secs(60))
        .redirects(8)
        .build();
    let res = agent.get(url).call().with_context(|| format!("fetching {url}"))?;
    let total: u64 = res
        .header("Content-Length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let mut reader = res.into_reader();
    let mut out = File::create(&part)?;
    let mut buf = vec![0u8; 1 << 20];
    let mut received = 0u64;
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        std::io::Write::write_all(&mut out, &buf[..n])?;
        received += n as u64;
        on_progress(received, total);
    }
    drop(out);
    if received < 1_000_000 {
        let _ = fs::remove_file(&part);
        bail!("{url} returned only {received} bytes");
    }
    fs::rename(&part, dest)?;
    Ok(())
}
