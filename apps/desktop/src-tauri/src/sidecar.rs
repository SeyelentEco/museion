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
    pub port: u16,
    /// The process, when Museion started it. `None` for a server that was
    /// already running on the port and was adopted.
    child: Option<Child>,
    /// Whether it dies with the app. False for adopted servers -- they were
    /// someone else's to begin with -- and for servers started under
    /// `MUSEION_KEEP_SERVICES`, which a developer wants to outlive one run
    /// of the app so the next run adopts them instead of reloading a 22 GB
    /// model. Everything else is owned, and the Job Object makes sure of it.
    owned: bool,
}

impl Sidecar {
    /// Is the process still alive? A model that failed to load exits rather than
    /// hanging, and the difference between "still loading" and "died" is the
    /// difference between waiting and reporting an error. An adopted server
    /// has no process handle; its port is the only witness.
    fn is_alive(&mut self) -> bool {
        match self.child.as_mut() {
            Some(c) => matches!(c.try_wait(), Ok(None)),
            None => true,
        }
    }

    /// Stop it, whatever way is available: the handle if there is one,
    /// otherwise whoever is listening on the port.
    fn kill(&mut self) {
        match self.child.as_mut() {
            Some(c) => {
                let _ = c.kill();
                let _ = c.wait();
            }
            None => kill_listener(self.port),
        }
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        // Best effort: if this is missed the GPU stays occupied.
        if self.owned {
            self.kill();
        }
    }
}

/// Set to leave the servers Museion starts running after it exits, so the
/// next launch adopts them. A developer's switch; it is never set for users.
pub fn keep_services() -> bool {
    std::env::var("MUSEION_KEEP_SERVICES").map(|v| v == "1" || v.eq_ignore_ascii_case("true")).unwrap_or(false)
}

/// Kill whatever process is listening on a local TCP port. Used only for
/// adopted servers, where there is no process handle.
fn kill_listener(port: u16) {
    #[cfg(windows)]
    {
        let out = Command::new("netstat").args(["-ano", "-p", "tcp"]).creation_flags(CREATE_NO_WINDOW).output();
        if let Ok(out) = out {
            let text = String::from_utf8_lossy(&out.stdout);
            let needle = format!(":{port} ");
            for line in text.lines() {
                if line.contains("LISTENING") && line.contains(&needle) {
                    if let Some(pid) = line.split_whitespace().last() {
                        let _ = Command::new("taskkill").args(["/PID", pid, "/F"]).creation_flags(CREATE_NO_WINDOW).output();
                    }
                }
            }
        }
    }
    #[cfg(not(windows))]
    {
        if let Ok(out) = Command::new("lsof").args(["-ti", &format!(":{port}")]).output() {
            for pid in String::from_utf8_lossy(&out.stdout).split_whitespace() {
                let _ = Command::new("kill").arg(pid).output();
            }
        }
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

    /// If a healthy server of this kind is already answering on the port,
    /// take it as ours to use -- not ours to kill -- and say so. For a
    /// llama-server, "of this kind" means it reports the expected model
    /// file; a chat model on the reranker's port, or last week's model, is
    /// not adopted. This is what makes a restart of the app cost seconds
    /// instead of a model reload, when the servers were left up.
    pub fn adopt(&self, role: Role, port: u16, health_url: &str, expect_model: Option<&Path>) -> bool {
        if self.is_running(role) {
            return true;
        }
        let agent = ureq_agent();
        let Ok(res) = agent.get(health_url).call() else { return false };
        if let Some(model) = expect_model {
            let Ok(body) = res.into_string() else { return false };
            let want = model.file_name().map(|f| f.to_string_lossy().to_lowercase()).unwrap_or_default();
            if want.is_empty() || !body.to_lowercase().contains(&want) {
                return false;
            }
        }
        if let Ok(mut running) = self.running.lock() {
            running.push(Sidecar { role, port, child: None, owned: false });
            return true;
        }
        false
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
        let owned = !keep_services();
        #[cfg(windows)]
        if owned {
            job::assign(&child);
        }

        self.running
            .lock()
            .map_err(|_| anyhow::anyhow!("supervisor lock poisoned"))?
            .push(Sidecar { role, port, child: Some(child), owned });
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

    /// Stop one service outright, adopted or not: a person pressing Stop, or
    /// a benchmark swapping the chat model, means it.
    pub fn stop(&self, role: Role) {
        if let Ok(mut running) = self.running.lock() {
            running.retain_mut(|s| {
                if s.role == role {
                    s.kill();
                    false
                } else {
                    true
                }
            });
        }
    }

    /// Stop everything, adopted or not. The Stop button.
    pub fn stop_all(&self) {
        if let Ok(mut running) = self.running.lock() {
            for s in running.iter_mut() {
                s.kill();
            }
            running.clear();
        }
    }

    /// The app is exiting: stop what is ours to stop. Adopted servers and
    /// servers started under MUSEION_KEEP_SERVICES are left running. Called
    /// when the window closes and again on exit, since leaving a chat model
    /// resident is the expensive mistake -- for servers Museion owns.
    pub fn release_all(&self) {
        if let Ok(mut running) = self.running.lock() {
            for s in running.iter_mut() {
                if s.owned {
                    s.kill();
                }
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
