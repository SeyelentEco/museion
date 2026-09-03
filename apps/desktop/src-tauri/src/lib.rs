//! Museion desktop: the shell around the retrieval core and its sidecars.

mod config;
mod sidecar;

use config::Config;
use museion_core::{Asker, Index, Llama};
use serde::Serialize;
use sidecar::{Role, Supervisor};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{Emitter, Manager, State};

pub struct AppState {
    config: Mutex<Config>,
    config_path: PathBuf,
    supervisor: Supervisor,
}

/// Errors cross the boundary to the UI as strings; anyhow's chain is preserved
/// so the cause is not lost on the way.
type UiResult<T> = Result<T, String>;

fn ui_err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

#[derive(Serialize)]
pub struct Status {
    configured: bool,
    library_dir: Option<String>,
    documents: usize,
    pages: usize,
    words: usize,
    kiwix_running: bool,
    chat_running: bool,
    reranker_running: bool,
    chat_model: Option<String>,
    reranker_model: Option<String>,
}

#[derive(Serialize)]
pub struct HardwareInfo {
    accelerator: String,
    memory_mb: u64,
    system_ram_mb: u64,
    tier: String,
    tier_detail: String,
}

#[derive(Serialize, Clone)]
pub struct Source {
    n: usize,
    title: String,
    author: String,
    page: usize,
    book: String,
    snippet: String,
}

#[tauri::command]
fn get_hardware() -> UiResult<HardwareInfo> {
    use museion_core::Accelerator;
    let hw = museion_core::detect_hardware().map_err(ui_err)?;
    let (accelerator, memory_mb) = match &hw.accelerator {
        Accelerator::Cuda { name, vram_mb } => (format!("{name} (CUDA)"), *vram_mb),
        Accelerator::Metal { name, unified_mb } => (format!("{name} (Metal)"), *unified_mb),
        Accelerator::Vulkan { name, vram_mb } => (format!("{name} (Vulkan)"), *vram_mb),
        Accelerator::Cpu => ("No usable GPU — CPU only".into(), 0),
    };
    Ok(HardwareInfo {
        accelerator,
        memory_mb,
        system_ram_mb: hw.system_ram_mb,
        tier: format!("{:?}", hw.tier),
        tier_detail: hw.tier.describe().to_string(),
    })
}

#[tauri::command]
fn get_status(state: State<AppState>) -> UiResult<Status> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let (documents, pages, words) = match cfg.index_path() {
        Some(p) if p.exists() => Index::open(&p)
            .and_then(|i| i.stats())
            .map(|s| (s.documents, s.pages, s.words))
            .unwrap_or((0, 0, 0)),
        _ => (0, 0, 0),
    };
    Ok(Status {
        configured: cfg.is_ready(),
        library_dir: cfg.library_dir.as_ref().map(|p| p.display().to_string()),
        documents,
        pages,
        words,
        kiwix_running: state.supervisor.is_running(Role::Kiwix),
        chat_running: state.supervisor.is_running(Role::Chat),
        reranker_running: state.supervisor.is_running(Role::Reranker),
        chat_model: cfg.chat_model.as_ref().map(|p| file_name(p)),
        reranker_model: cfg.reranker_model.as_ref().map(|p| file_name(p)),
    })
}

fn file_name(p: &std::path::Path) -> String {
    p.file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| p.display().to_string())
}

#[tauri::command]
fn get_config(state: State<AppState>) -> UiResult<Config> {
    Ok(state.config.lock().map_err(ui_err)?.clone())
}

#[tauri::command]
fn set_config(state: State<AppState>, config: Config) -> UiResult<()> {
    config.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = config;
    Ok(())
}

/// Bring up the sidecars. Reports progress as events so the UI can say what it
/// is waiting for -- a large model can take minutes, and silence during that
/// looks like a hang.
#[tauri::command]
async fn start_services(app: tauri::AppHandle, state: State<'_, AppState>) -> UiResult<()> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let kiwix_exe = cfg.kiwix_exe.clone().ok_or("kiwix-serve path not configured")?;
    let llama_exe = cfg.llama_exe.clone().ok_or("llama-server path not configured")?;
    let library = cfg.library_dir.clone().ok_or("no library configured")?;

    let say = |msg: &str| {
        let _ = app.emit("service-progress", msg.to_string());
    };

    say("Starting the content server…");
    state
        .supervisor
        .start_kiwix(&kiwix_exe, &library, cfg.kiwix_port)
        .map_err(ui_err)?;
    state
        .supervisor
        .wait_ready(
            Role::Kiwix,
            &format!("http://127.0.0.1:{}/catalog/v2/entries", cfg.kiwix_port),
            Duration::from_secs(60),
        )
        .map_err(ui_err)?;

    if let Some(model) = &cfg.reranker_model {
        say("Starting the reranker…");
        // GPU layers deliberately 0: on a 12 GB card the chat model wants all of
        // it, and this costs only seconds on CPU.
        let args: Vec<String> = ["--reranking", "--pooling", "rank", "--batch-size", "2048",
                                 "--ubatch-size", "2048", "--ctx-size", "8192"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        state
            .supervisor
            .start_llama(
                Role::Reranker,
                &llama_exe,
                model,
                cfg.reranker_port,
                0,
                &args,
            )
            .map_err(ui_err)?;
        state
            .supervisor
            .wait_ready(
                Role::Reranker,
                &format!("http://127.0.0.1:{}/v1/models", cfg.reranker_port),
                Duration::from_secs(180),
            )
            .map_err(ui_err)?;
    }

    let model = cfg.chat_model.clone().ok_or("no chat model configured")?;
    say("Loading the chat model — this can take a few minutes…");
    let mut args = cfg.chat_args.clone();
    for a in ["--flash-attn", "on", "--jinja", "--parallel", "1",
              "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"] {
        args.push(a.to_string());
    }
    state
        .supervisor
        .start_llama(
            Role::Chat,
            &llama_exe,
            &model,
            cfg.chat_port,
            cfg.chat_gpu_layers,
            &args,
        )
        .map_err(ui_err)?;
    state
        .supervisor
        .wait_ready(
            Role::Chat,
            &format!("http://127.0.0.1:{}/v1/models", cfg.chat_port),
            Duration::from_secs(600),
        )
        .map_err(ui_err)?;

    say("Ready.");
    Ok(())
}

#[tauri::command]
fn stop_services(state: State<AppState>) -> UiResult<()> {
    state.supervisor.stop_all();
    Ok(())
}

/// Ask a question. Answer tokens arrive as "answer-token" events; the sources
/// come back as the return value once generation finishes.
#[tauri::command]
async fn ask(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    question: String,
) -> UiResult<Vec<Source>> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let index_path = cfg.index_path().ok_or("no library configured")?;
    let index = Index::open(&index_path).map_err(ui_err)?;

    let chat = Llama::new(&format!("http://127.0.0.1:{}", cfg.chat_port));
    let reranker = Llama::new(&format!("http://127.0.0.1:{}", cfg.reranker_port));
    let use_rerank = reranker.is_up();

    let asker = Asker {
        index: &index,
        chat: &chat,
        reranker: if use_rerank { Some(&reranker) } else { None },
    };

    let answer = asker
        .ask(&question, |t| {
            let _ = app.emit("answer-token", t.to_string());
        })
        .map_err(ui_err)?;

    // With no sources nothing streamed, and the refusal itself is the answer --
    // it must still reach the UI.
    if answer.sources.is_empty() {
        let _ = app.emit("answer-token", answer.text.clone());
    }

    Ok(answer
        .sources
        .iter()
        .enumerate()
        .map(|(i, h)| Source {
            n: i + 1,
            title: h.doc_title.clone(),
            author: h.author.clone(),
            page: h.page,
            book: h.book.clone(),
            snippet: h.snippet.clone(),
        })
        .collect())
}

/// Build the search index over the library. Needs the content server running,
/// which start_services arranges. Progress goes out as "index-progress" events
/// -- 155 documents take a few minutes, and a person should see which one it
/// is on rather than a frozen button.
#[tauri::command]
async fn build_index(app: tauri::AppHandle, state: State<'_, AppState>, force: bool) -> UiResult<String> {
    use museion_core::{indexer::Outcome, Progress};
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let library = cfg.library_dir.clone().ok_or("no library configured")?;
    let db = cfg.index_path().ok_or("no library configured")?;
    let pdfium = cfg
        .pdfium_dir
        .clone()
        .or_else(|| cfg.kiwix_exe.as_ref().and_then(|p| p.parent().map(|d| d.to_path_buf())))
        .ok_or("pdfium directory not configured")?;
    if !state.supervisor.is_running(Role::Kiwix) {
        return Err("the content server is not running -- start services first".into());
    }
    let kiwix_url = format!("http://127.0.0.1:{}", cfg.kiwix_port);

    let say = |msg: String| {
        let _ = app.emit("index-progress", msg);
    };
    let opts = museion_core::IndexOptions {
        library: &library,
        db: &db,
        kiwix_url: &kiwix_url,
        pdfium_dir: &pdfium,
        force,
    };
    let stats = museion_core::index_library(&opts, |p| match p {
        Progress::Archive { book, documents } => say(format!("Indexing {book} ({documents} documents)…")),
        Progress::Native { book } => say(format!("{book}: uses its own index")),
        Progress::Skipped { book } => say(format!("{book}: already indexed")),
        Progress::Document { done, total, title, outcome, .. } => {
            if outcome != Outcome::Duplicate {
                say(format!("{done}/{total}  {title}"));
            }
        }
        Progress::ArchiveDone { book, indexed, .. } => say(format!("{book}: {indexed} documents indexed")),
    })
    .map_err(ui_err)?;

    Ok(format!(
        "{} documents, {} pages, {} words indexed.",
        stats.documents, stats.pages, stats.words
    ))
}

/// Measure the chat model against this machine and store the offload split.
#[tauri::command]
async fn fit_chat_model(state: State<'_, AppState>) -> UiResult<String> {
    let cfg = state.config.lock().map_err(ui_err)?.clone();
    let model = cfg.chat_model.clone().ok_or("no chat model configured")?;
    let llama = cfg.llama_exe.clone().ok_or("llama-server path not configured")?;
    let fit_exe = llama
        .parent()
        .map(|d| d.join(if cfg!(windows) { "llama-fit-params.exe" } else { "llama-fit-params" }))
        .ok_or("cannot locate llama-fit-params next to llama-server")?;

    // Context comes from whatever --ctx-size is already configured, so the
    // fit is measured for the configuration that will actually run.
    let ctx = cfg
        .chat_args
        .windows(2)
        .find(|w| w[0] == "--ctx-size" || w[0] == "-c")
        .and_then(|w| w[1].parse().ok())
        .unwrap_or(32768);

    let fit = museion_core::fit_model(&fit_exe, &model, ctx).map_err(ui_err)?;

    // Keep any non-fit arguments the user had (like --ctx-size), replace the
    // fitted ones. The fit already includes -c, so drop ours to avoid a repeat.
    let mut args: Vec<String> = Vec::new();
    let mut skip = false;
    for a in &cfg.chat_args {
        if skip {
            skip = false;
            continue;
        }
        if a == "--ctx-size" || a == "-c" || a == "-ngl" || a == "--n-gpu-layers" || a == "-ot" {
            skip = true;
            continue;
        }
        args.push(a.clone());
    }
    let mut fitted = fit.args.clone();
    // -ngl is passed by the supervisor from chat_gpu_layers; strip it here.
    let mut i = 0;
    while i < fitted.len() {
        if fitted[i] == "-ngl" || fitted[i] == "--n-gpu-layers" {
            fitted.drain(i..(i + 2).min(fitted.len()));
        } else {
            i += 1;
        }
    }
    args.extend(fitted);

    let mut updated = cfg.clone();
    updated.chat_args = args;
    if let Some(n) = fit.gpu_layers {
        updated.chat_gpu_layers = n;
    }
    updated.save(&state.config_path).map_err(ui_err)?;
    *state.config.lock().map_err(ui_err)? = updated;

    Ok(format!(
        "{} layers on the GPU; {}",
        fit.gpu_layers.map(|n| n.to_string()).unwrap_or_else(|| "all".into()),
        if fit.args.iter().any(|a| a == "-ot") {
            "expert tensors split between GPU and CPU."
        } else {
            "everything resident on the GPU."
        }
    ))
}

#[tauri::command]
fn kiwix_url(state: State<AppState>) -> UiResult<String> {
    let cfg = state.config.lock().map_err(ui_err)?;
    Ok(format!("http://127.0.0.1:{}", cfg.kiwix_port))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let dir = app.path().app_config_dir()?;
            let config_path = dir.join("config.json");
            let config = Config::load(&config_path);
            app.manage(AppState {
                config: Mutex::new(config),
                config_path,
                supervisor: Supervisor::new(),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_hardware,
            get_status,
            get_config,
            set_config,
            start_services,
            stop_services,
            ask,
            build_index,
            fit_chat_model,
            kiwix_url,
        ])
        .build(tauri::generate_context!())
        .expect("failed to start Museion")
        .run(|app, event| {
            // Both hooks matter. Closing the last window does not necessarily
            // end the process, and a chat model left resident holds the whole
            // GPU until someone kills it by hand.
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                if let Some(state) = app.try_state::<AppState>() {
                    state.supervisor.stop_all();
                }
            }
        });
}
