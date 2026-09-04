//! How much room there is, and how much the library is using.
//!
//! A person deciding whether to fetch Wikipedia needs one number more than any
//! other: whether it will fit. Free space straight from the OS, no crate.

use anyhow::{bail, Context, Result};
use std::path::Path;

/// Bytes free on the volume holding `path`, for the current user.
#[cfg(windows)]
pub fn free_space(path: &Path) -> Result<u64> {
    use std::os::windows::ffi::OsStrExt;
    extern "system" {
        fn GetDiskFreeSpaceExW(
            dir: *const u16,
            free_to_caller: *mut u64,
            total: *mut u64,
            total_free: *mut u64,
        ) -> i32;
    }
    // The path must exist; walk up to the nearest existing ancestor so a
    // library folder that has not been created yet still gets an answer.
    let mut p = path;
    while !p.exists() {
        p = p.parent().context("no existing ancestor")?;
    }
    let wide: Vec<u16> = p.as_os_str().encode_wide().chain(std::iter::once(0)).collect();
    let (mut free, mut total, mut total_free) = (0u64, 0u64, 0u64);
    // SAFETY: a valid NUL-terminated wide string and three out-pointers.
    let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut free, &mut total, &mut total_free) };
    if ok == 0 {
        bail!("could not read free space for {}", path.display());
    }
    Ok(free)
}

#[cfg(not(windows))]
pub fn free_space(path: &Path) -> Result<u64> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    #[repr(C)]
    struct StatVfs {
        f_bsize: u64,
        f_frsize: u64,
        f_blocks: u64,
        f_bfree: u64,
        f_bavail: u64,
        f_files: u64,
        f_ffree: u64,
        f_favail: u64,
        f_fsid: u64,
        f_flag: u64,
        f_namemax: u64,
        _spare: [u64; 6],
    }
    extern "C" {
        fn statvfs(path: *const std::ffi::c_char, buf: *mut StatVfs) -> i32;
    }
    let mut p = path;
    while !p.exists() {
        p = p.parent().context("no existing ancestor")?;
    }
    let c = CString::new(p.as_os_str().as_bytes())?;
    let mut s: StatVfs = unsafe { std::mem::zeroed() };
    // SAFETY: valid C string and a zeroed, correctly sized buffer.
    if unsafe { statvfs(c.as_ptr(), &mut s) } != 0 {
        bail!("could not read free space for {}", path.display());
    }
    Ok(s.f_bavail.saturating_mul(s.f_frsize))
}

/// Total bytes of files under `dir`, recursing a few levels. Symlinks are
/// not followed.
pub fn dir_size(dir: &Path) -> u64 {
    fn walk(d: &Path, depth: usize) -> u64 {
        if depth > 3 {
            return 0;
        }
        let Ok(rd) = std::fs::read_dir(d) else { return 0 };
        rd.flatten()
            .map(|e| match e.metadata() {
                Ok(m) if m.is_file() => m.len(),
                Ok(m) if m.is_dir() => walk(&e.path(), depth + 1),
                _ => 0,
            })
            .sum()
    }
    walk(dir, 0)
}

/// A short name for the volume: "E:" on Windows, "/" or the mount otherwise.
pub fn volume_name(path: &Path) -> String {
    let s = path.display().to_string();
    if s.len() >= 2 && s.as_bytes()[1] == b':' {
        return s[..2].to_uppercase();
    }
    "/".into()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn free_space_of_the_temp_dir_is_a_real_number() {
        let f = free_space(&std::env::temp_dir()).unwrap();
        assert!(f > 0);
    }

    #[test]
    fn free_space_for_a_folder_that_does_not_exist_yet_uses_its_parent() {
        let p = std::env::temp_dir().join("museion-not-created-yet").join("deeper");
        assert!(free_space(&p).unwrap() > 0);
    }

    #[test]
    fn dir_size_counts_files() {
        let d = std::env::temp_dir().join(format!("museion-size-{}", std::process::id()));
        std::fs::create_dir_all(d.join("sub")).unwrap();
        std::fs::write(d.join("a.bin"), vec![0u8; 1000]).unwrap();
        std::fs::write(d.join("sub").join("b.bin"), vec![0u8; 500]).unwrap();
        assert_eq!(dir_size(&d), 1500);
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn volume_names() {
        assert_eq!(volume_name(Path::new(r"E:\museion-library")), "E:");
        assert_eq!(volume_name(Path::new("/home/x")), "/");
    }
}
