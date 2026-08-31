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

    /// Start kiwix-serve over every archive in the library.
    pub fn start_kiwix(&self, exe: &Path, library: &Path, port: u16) -> Result<()> {
        if self.is_running(Role::Kiwix) {
            return Ok(());
        }
        let zims = list_zims(library)?;
        if zims.is_empty() {
            bail!(
                "no .zim archives in {}. Fetch some content first.",
                library.display()
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
