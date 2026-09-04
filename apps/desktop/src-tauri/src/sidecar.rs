//! Supervising the helper processes: kiwix-serve for content, llama-server for
//! the chat model and again for the reranker.
//!
//! Shutdown is the part that matters. A loaded chat model holds essentially all
//! of the GPU's memory for as long as it lives -- 11.5 GB of 12 on the machine
//! this was developed against -- so a sidecar that outlives the app does not
//! merely leak a process, it makes the GPU unusable for everything else until
//! someone notices and kills it by hand.
//!
//! The classic way to get this wrong is to launch the server through a wrapper
//! script and then stop the script: the server is a *grandchild*, survives, and
//! keeps every byte. Museion therefore executes the server binary directly, so
//! the handle it holds is the process that actually owns the memory.

use anyhow::{bail, Context, Result};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Stops a console window flashing up on Windows every time a sidecar starts.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Kiwix,
    Chat,
    Reranker,
}

impl Role {
    pub fn label(&self) -> &'static str {
        match self {
            Role::Kiwix => "content server",
            Role::Chat => "chat model",
            Role::Reranker => "reranker",
        }
    }
}

pub struct Sidecar {
    pub role: Role,
    #[allow(dead_code)]
    pub port: u16,
    child: Child,
}

impl Sidecar {
    /// Is the process still alive? A model that failed to load exits rather than
    /// hanging, and the difference between "still loading" and "died" is the
    /// difference between waiting and reporting an error.
    fn is_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        // Best effort: if this is missed the GPU stays occupied.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[derive(Default)]
pub struct Supervisor {
    running: Mutex<Vec<Sidecar>>,
}

impl Supervisor {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_running(&self, role: Role) -> bool {
        self.running
            .lock()
            .map(|r| r.iter().any(|s| s.role == role))
            .unwrap_or(false)
    }

    /// Part of the supervisor's interface rather than of any current caller:
    /// restarting a single service (swapping the chat model without dropping
    /// kiwix-serve) is the obvious next use.
    #[allow(dead_code)]
    pub fn port_for(&self, role: Role) -> Option<u16> {
        self.running
            .lock()
            .ok()?
            .iter()
            .find(|s| s.role == role)
            .map(|s| s.port)
    }

    /// Start kiwix-serve over every archive in the given folders.
    pub fn start_kiwix(&self, exe: &Path, dirs: &[PathBuf], port: u16) -> Result<()> {
        if self.is_running(Role::Kiwix) {
            return Ok(());
        }
        let mut zims = Vec::new();
        for d in dirs {
            if let Ok(found) = list_zims(d) {
                zims.extend(found);
            }
        }
        if zims.is_empty() {
            bail!(
                "no .zim archives in {}. Fetch some content first.",
                dirs.iter().map(|d| d.display().to_string()).collect::<Vec<_>>().join(", ")
            );
        }
        let mut cmd = Command::new(exe);
        cmd.arg("--port").arg(port.to_string());
        for z in &zims {
            cmd.arg(z);
        }
        self.spawn(Role::Kiwix, cmd, port, exe)
    }

    /// Start a llama-server instance.
    ///
    /// `gpu_layers` of 0 keeps a model on the CPU, which is how the reranker is
    /// run: a 0.6B cross-encoder costs a couple of seconds there, and that is a
    /// far better trade than taking VRAM away from the model writing the answer.
    pub fn start_llama(
        &self,
        role: Role,
        exe: &Path,
        model: &Path,
        port: u16,
        gpu_layers: u32,
        extra: &[String],
    ) -> Result<()> {
        if self.is_running(role) {
            return Ok(());
        }
        if !model.exists() {
            bail!("model not found: {}", model.display());
        }
        let mut cmd = Command::new(exe);
        cmd.arg("--model")
            .arg(model)
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(port.to_string())
            .arg("--n-gpu-layers")
            .arg(gpu_layers.to_string());
        for a in extra {
            cmd.arg(a);
        }
        self.spawn(role, cmd, port, exe)
    }

    fn spawn(&self, role: Role, mut cmd: Command, port: u16, exe: &Path) -> Result<()> {
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);

        let child = cmd
            .spawn()
            .with_context(|| format!("starting {} ({})", role.label(), exe.display()))?;

        // Tie the child's life to ours at the OS level. The exit hooks only
        // run on a graceful quit; a crash, a kill from Task Manager, or an
        // out-of-memory abort skips them, and then a chat model sits on the
        // whole GPU with nothing left to stop it. Measured: force-killing the
        // app left three sidecars alive holding 11.7 GB. A Job Object with
        // kill-on-close has no such gap -- when the last handle to the job
        // goes away with this process, Windows terminates everything in it.
        #[cfg(windows)]
        job::assign(&child);

        self.running
            .lock()
            .map_err(|_| anyhow::anyhow!("supervisor lock poisoned"))?
            .push(Sidecar { role, port, child });
        Ok(())
    }

    /// Wait until the service answers, or until it gives up.
    ///
    /// A large mixture-of-experts model can take minutes to load -- 200 seconds
    /// for a 20 GB one from a spinning disk -- so the timeout is generous. The
    /// process is checked as well as the port, so a model that fails to load is
    /// reported immediately instead of waiting out the full timeout.
    pub fn wait_ready(&self, role: Role, health_url: &str, timeout: Duration) -> Result<()> {
        let started = Instant::now();
        let agent = ureq_agent();
        while started.elapsed() < timeout {
            {
                let mut guard = self
                    .running
                    .lock()
                    .map_err(|_| anyhow::anyhow!("supervisor lock poisoned"))?;
                match guard.iter_mut().find(|s| s.role == role) {
                    None => bail!("{} is not running", role.label()),
                    Some(s) => {
                        if !s.is_alive() {
                            bail!(
                                "{} exited while starting up. The usual causes are a model \
                                 too large for the available memory, or a missing GPU runtime.",
                                role.label()
                            );
                        }
                    }
                }
            }
            if agent.get(health_url).call().is_ok() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(750));
        }
        bail!(
            "{} did not become ready within {}s",
            role.label(),
            timeout.as_secs()
        )
    }

    #[allow(dead_code)]
    pub fn stop(&self, role: Role) {
        if let Ok(mut running) = self.running.lock() {
            running.retain_mut(|s| {
                if s.role == role {
                    let _ = s.child.kill();
                    let _ = s.child.wait();
                    false
                } else {
                    true
                }
            });
        }
    }

    /// Stop everything. Called when the window closes and again on exit, since
    /// leaving a chat model resident is the expensive mistake.
    pub fn stop_all(&self) {
        if let Ok(mut running) = self.running.lock() {
            for s in running.iter_mut() {
                let _ = s.child.kill();
                let _ = s.child.wait();
            }
            running.clear();
        }
    }
}

/// A single process-wide Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
/// Declared directly against kernel32, which std already links, so no crate.
#[cfg(windows)]
mod job {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use std::sync::OnceLock;

    type Handle = *mut std::ffi::c_void;

    #[repr(C)]
    struct JobObjectBasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }

    #[repr(C)]
    struct IoCounters {
        read_operation_count: u64,
        write_operation_count: u64,
        other_operation_count: u64,
        read_transfer_count: u64,
        write_transfer_count: u64,
        other_transfer_count: u64,
    }

    #[repr(C)]
    struct JobObjectExtendedLimitInformation {
        basic: JobObjectBasicLimitInformation,
        io_info: IoCounters,
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }

    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: u32 = 9;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x2000;

    extern "system" {
        fn CreateJobObjectW(attrs: *mut std::ffi::c_void, name: *const u16) -> Handle;
        fn SetInformationJobObject(job: Handle, class: u32, info: *mut std::ffi::c_void, len: u32) -> i32;
        fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
    }

    struct SendHandle(Handle);
    // SAFETY: a job handle is a plain kernel handle, usable from any thread.
    unsafe impl Send for SendHandle {}
    unsafe impl Sync for SendHandle {}

    static JOB: OnceLock<Option<SendHandle>> = OnceLock::new();

    fn job() -> Option<Handle> {
        JOB.get_or_init(|| {
            // SAFETY: plain Win32 calls with valid arguments; the info struct
            // is repr(C) with the documented layout.
            unsafe {
                let h = CreateJobObjectW(std::ptr::null_mut(), std::ptr::null());
                if h.is_null() {
                    return None;
                }
                let mut info: JobObjectExtendedLimitInformation = std::mem::zeroed();
                info.basic.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    h,
                    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    &mut info as *mut _ as *mut std::ffi::c_void,
                    std::mem::size_of::<JobObjectExtendedLimitInformation>() as u32,
                );
                if ok == 0 {
                    return None;
                }
                Some(SendHandle(h))
            }
        })
        .as_ref()
        .map(|h| h.0)
    }

    /// Best effort: if the job cannot be created the child still runs, and
    /// the graceful-quit path still stops it.
    pub fn assign(child: &Child) {
        if let Some(j) = job() {
            // SAFETY: both handles are valid for the duration of the call.
            unsafe {
                AssignProcessToJobObject(j, child.as_raw_handle() as Handle);
            }
        }
    }
}

fn ureq_agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_millis(800))
        .timeout(Duration::from_secs(3))
        .build()
}

fn list_zims(library: &Path) -> Result<Vec<PathBuf>> {
    let mut out: Vec<PathBuf> = std::fs::read_dir(library)
        .with_context(|| format!("reading {}", library.display()))?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "zim"))
        .collect();
    out.sort();
    Ok(out)
}
