//! Working out what the machine can run.
//!
//! The instinct on a mid-range GPU is to pick the largest dense model that
//! fits in VRAM. That is usually wrong. A sparse mixture-of-experts model with
//! its expert tensors offloaded to system RAM beats a dense model that merely
//! fits, because only a fraction of its parameters fire per token: measured on
//! one 12 GB card, a 35B-A3B MoE ran at 43.6 tok/s against 30.6 for a dense
//! 12B that sat entirely in VRAM. Bigger and faster at once.
//!
//! So the tiers below reach for MoE architectures rather than the largest dense
//! model that fits. See docs/hardware.md.

use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub enum Accelerator {
    /// NVIDIA, addressed through CUDA.
    Cuda { name: String, vram_mb: u64 },
    /// Apple Silicon, addressed through Metal against unified memory.
    Metal { name: String, unified_mb: u64 },
    /// Anything else with a GPU worth using: AMD, Intel, via Vulkan.
    Vulkan { name: String, vram_mb: u64 },
    /// No usable GPU. Still works; just slowly.
    Cpu,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Hardware {
    pub accelerator: Accelerator,
    pub system_ram_mb: u64,
    pub tier: Tier,
}

/// Which class of model this machine should run.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub enum Tier {
    /// CPU only, or under ~6 GB of VRAM. A small dense model.
    Modest,
    /// 8-12 GB. A ~30B-A3B-class MoE with its experts offloaded to RAM.
    Capable,
    /// 16-24 GB. The same MoE with more resident, and a longer context.
    Generous,
    /// 32 GB+ of unified memory, or a very large card.
    Ample,
}

impl Tier {
    pub fn describe(&self) -> &'static str {
        match self {
            Tier::Modest => "a small model, or CPU-only inference",
            Tier::Capable => "a sparse MoE with expert tensors offloaded to system RAM",
            Tier::Generous => "a sparse MoE, more layers resident, longer context",
            Tier::Ample => "a large MoE",
        }
    }
}

/// Usable VRAM in MB, or unified memory for Apple Silicon.
pub fn accelerator_memory_mb(a: &Accelerator) -> u64 {
    match a {
        Accelerator::Cuda { vram_mb, .. } | Accelerator::Vulkan { vram_mb, .. } => *vram_mb,
        Accelerator::Metal { unified_mb, .. } => *unified_mb,
        Accelerator::Cpu => 0,
    }
}

pub fn detect() -> Result<Hardware> {
    let accelerator = detect_accelerator();
    let system_ram_mb = detect_system_ram_mb();
    let tier = pick_tier(&accelerator, system_ram_mb);
    Ok(Hardware {
        accelerator,
        system_ram_mb,
        tier,
    })
}

/// Tier from available memory.
///
/// System RAM matters as well as VRAM: expert offload only works if the experts
/// have somewhere to live. A 12 GB card with 8 GB of system RAM cannot run the
/// tier that makes that card worth having.
pub fn pick_tier(accel: &Accelerator, system_ram_mb: u64) -> Tier {
    let vram = accelerator_memory_mb(accel);

    if let Accelerator::Metal { unified_mb, .. } = accel {
        // Unified memory is shared with the whole system, so the usable share
        // is smaller than the number suggests.
        return match unified_mb {
            m if *m >= 32_000 => Tier::Ample,
            m if *m >= 24_000 => Tier::Generous,
            m if *m >= 16_000 => Tier::Capable,
            _ => Tier::Modest,
        };
    }

    match vram {
        v if v >= 32_000 => Tier::Ample,
        v if v >= 16_000 && system_ram_mb >= 16_000 => Tier::Generous,
        // Expert offload needs room in system RAM for the experts.
        v if v >= 8_000 && system_ram_mb >= 24_000 => Tier::Capable,
        _ => Tier::Modest,
    }
}

fn detect_accelerator() -> Accelerator {
    if cfg!(target_os = "macos") {
        if let Some(mb) = macos_unified_memory_mb() {
            let name = sysctl("machdep.cpu.brand_string").unwrap_or_else(|| "Apple Silicon".into());
            // Metal is only the right answer on Apple Silicon; Intel Macs fall
            // through to CPU, which is what they should use anyway.
            if name.contains("Apple") {
                return Accelerator::Metal {
                    name,
                    unified_mb: mb,
                };
            }
        }
        return Accelerator::Cpu;
    }

    if let Some((name, vram_mb)) = nvidia_smi() {
        return Accelerator::Cuda { name, vram_mb };
    }
    if let Some((name, vram_mb)) = other_gpu() {
        return Accelerator::Vulkan { name, vram_mb };
    }
    Accelerator::Cpu
}

/// Ask nvidia-smi. Absent or failing means no usable NVIDIA GPU, which is a
/// normal answer and not an error.
fn nvidia_smi() -> Option<(String, u64)> {
    let out = Command::new("nvidia-smi")
        .args([
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let line = text.lines().next()?;
    let (name, mem) = line.split_once(',')?;
    Some((name.trim().to_string(), mem.trim().parse().ok()?))
}

#[cfg(target_os = "windows")]
fn other_gpu() -> Option<(String, u64)> {
    // AdapterRAM is a 32-bit field and wraps above 4 GB, so it is used only to
    // establish that a GPU exists; the tier for such cards is conservative
    // regardless.
    let out = Command::new("wmic")
        .args(["path", "Win32_VideoController", "get", "Name,AdapterRAM", "/format:csv"])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    for line in text.lines().skip(1) {
        let cols: Vec<&str> = line.split(',').collect();
        if cols.len() >= 3 {
            if let Ok(bytes) = cols[1].trim().parse::<u64>() {
                let name = cols[2].trim();
                if !name.is_empty() && bytes > 0 {
                    return Some((name.to_string(), bytes / 1024 / 1024));
                }
            }
        }
    }
    None
}

#[cfg(not(target_os = "windows"))]
fn other_gpu() -> Option<(String, u64)> {
    None
}

fn macos_unified_memory_mb() -> Option<u64> {
    sysctl("hw.memsize")?.parse::<u64>().ok().map(|b| b / 1024 / 1024)
}

fn sysctl(key: &str) -> Option<String> {
    let out = Command::new("sysctl").args(["-n", key]).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// System RAM straight from the Win32 API.
///
/// This used to shell out to `wmic`, which reported 65 GB from the command
/// line and 0 MB from inside the app -- dropping a 12 GB card to the Modest
/// tier and fetching the wrong model. wmic is also deprecated and absent on
/// current Windows 11 builds, and writes UTF-16 to a pipe in some contexts.
/// GlobalMemoryStatusEx has none of those problems and needs no process.
#[cfg(target_os = "windows")]
fn detect_system_ram_mb() -> u64 {
    #[repr(C)]
    struct MemoryStatusEx {
        length: u32,
        memory_load: u32,
        total_phys: u64,
        avail_phys: u64,
        total_page_file: u64,
        avail_page_file: u64,
        total_virtual: u64,
        avail_virtual: u64,
        avail_extended_virtual: u64,
    }
    extern "system" {
        fn GlobalMemoryStatusEx(buffer: *mut MemoryStatusEx) -> i32;
    }
    let mut m = MemoryStatusEx {
        length: std::mem::size_of::<MemoryStatusEx>() as u32,
        memory_load: 0,
        total_phys: 0,
        avail_phys: 0,
        total_page_file: 0,
        avail_page_file: 0,
        total_virtual: 0,
        avail_virtual: 0,
        avail_extended_virtual: 0,
    };
    // SAFETY: the struct is repr(C) with the documented layout and its length
    // field is set, which is all the call requires.
    let ok = unsafe { GlobalMemoryStatusEx(&mut m) };
    if ok == 0 {
        0
    } else {
        m.total_phys / 1024 / 1024
    }
}

#[cfg(not(target_os = "windows"))]
fn detect_system_ram_mb() -> u64 {
    if let Some(v) = sysctl("hw.memsize").and_then(|v| v.parse::<u64>().ok()) {
        return v / 1024 / 1024;
    }
    std::fs::read_to_string("/proc/meminfo")
        .ok()
        .and_then(|s| {
            s.lines()
                .find(|l| l.starts_with("MemTotal:"))
                .and_then(|l| l.split_whitespace().nth(1).and_then(|v| v.parse::<u64>().ok()))
        })
        .map(|kb| kb / 1024)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expert_offload_needs_system_ram_not_just_vram() {
        let card = Accelerator::Cuda {
            name: "RTX 3060".into(),
            vram_mb: 12_288,
        };
        // The whole point of the Capable tier is holding expert tensors in RAM.
        assert_eq!(pick_tier(&card, 64_000), Tier::Capable);
        // Same card, not enough RAM to hold them: do not promise what it cannot do.
        assert_eq!(pick_tier(&card, 8_000), Tier::Modest);
    }

    #[test]
    fn a_machine_with_no_gpu_still_gets_a_tier() {
        assert_eq!(pick_tier(&Accelerator::Cpu, 32_000), Tier::Modest);
    }

    #[test]
    fn apple_silicon_is_judged_on_unified_memory() {
        let m = |mb| Accelerator::Metal { name: "Apple M3".into(), unified_mb: mb };
        assert_eq!(pick_tier(&m(8_000), 8_000), Tier::Modest);
        assert_eq!(pick_tier(&m(18_000), 18_000), Tier::Capable);
        assert_eq!(pick_tier(&m(64_000), 64_000), Tier::Ample);
    }
}
