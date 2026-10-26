//! museion -- index and search the offline library from the command line.
//!
//! The same code the desktop app uses, exposed as a CLI so retrieval can be
//! measured and regression-tested without a GUI in the way.
//!
//!   museion index  --library E:\museion-library --db library.db
//!   museion search --db library.db "how do I purify water"
//!   museion status --db library.db

use anyhow::{bail, Context, Result};
use museion_core::{Asker, Index, Llama};
use std::io::Write;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

const USAGE: &str = "\
museion -- offline library index and search

  museion index  --library <dir> [--extra <dir>]... [--db <file>] [--kiwix <url>] [--force]
  museion search --db <file> <query...> [--limit N]
  museion ask    --db <file> <question...> [--llama <url>] [--reranker <url>]
  museion chat   --db <file>                [--llama <url>] [--reranker <url>]
  museion prepare --dest <dir> --bundle <id> [--models a,b] [--accelerators cuda,vulkan,cpu]
                  [--app <exe>] [--installer <exe>] [--no-index] [--no-webview2] [--skeleton]
  museion status --db <file>
  museion hardware
  museion eval   --db <file> [--label <name>] [--only <capability|horizon|id>]
  museion fit    --model <gguf> [--fit-exe <path>] [--ctx N]
  museion tools  --dest <dir> [--cpu]

Options
  --library <dir>   Directory holding .zim archives
  --db <file>       Index file (default: <library>/museion-index.db)
  --kiwix <url>     kiwix-serve base URL (default: http://127.0.0.1:8090)
  --pdfium <dir>    Directory holding the pdfium library (default: ./bin)
  --llama <url>     chat model server (default: http://127.0.0.1:8080)
  --reranker <url>  reranker server (default: http://127.0.0.1:8081)
  --no-rerank       Skip reranking, use keyword ranking alone
  --tuning <file>   Prompts and limits override (see crates/museion-core/tuning.yaml)
  --force           Re-index archives that are already present
  --limit N         Number of results (default 10)
";

struct Args {
    cmd: String,
    rest: Vec<String>,
    flags: HashMap<String, String>,
}

fn parse_args() -> Args {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut rest = Vec::new();
    let mut flags = HashMap::new();
    let mut i = 0;
    while i < argv.len() {
        let a = &argv[i];
        if let Some(name) = a.strip_prefix("--") {
            if let Some((k, v)) = name.split_once('=') {
                flags.insert(k.to_string(), v.to_string());
            } else if argv.get(i + 1).is_some_and(|n| !n.starts_with("--")) {
                flags.insert(name.to_string(), argv[i + 1].clone());
                i += 1;
            } else {
                flags.insert(name.to_string(), "true".into());
            }
        } else {
            rest.push(a.clone());
        }
        i += 1;
    }
    let cmd = if rest.is_empty() {
        String::new()
    } else {
        rest.remove(0)
    };
    Args { cmd, rest, flags }
}

fn db_path(args: &Args) -> Result<PathBuf> {
    if let Some(p) = args.flags.get("db") {
        return Ok(PathBuf::from(p));
    }
    if let Some(l) = args.flags.get("library") {
        return Ok(Path::new(l).join("museion-index.db"));
    }
    bail!("need --db (or --library, so the default location can be derived)")
}

fn main() {
    if let Err(e) = run() {
        eprintln!("\nerror: {e:#}\n");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let args = parse_args();
    match args.cmd.as_str() {
        "index" => cmd_index(&args),
        "search" => cmd_search(&args),
        "ask" => cmd_ask(&args),
        "chat" => cmd_chat(&args),
        "prepare" => cmd_prepare(&args),
        "gather" => cmd_gather(&args),
        "hardware" => cmd_hardware(),
        "eval" => cmd_eval(&args),
        "fit" => cmd_fit(&args),
        "tools" => cmd_tools(&args),
        "status" => cmd_status(&args),
        "" | "help" | "-h" => {
            println!("{USAGE}");
            Ok(())
        }
        other => {
            eprintln!("unknown command {other:?}\n{USAGE}");
            std::process::exit(1);
        }
    }
}

fn cmd_index(args: &Args) -> Result<()> {
    use museion_core::indexer::Outcome;
    let library = args.flags.get("library").context("--library is required")?.clone();
    let kiwix_url = args.flags.get("kiwix").cloned().unwrap_or_else(|| "http://127.0.0.1:8090".into());
    let pdfium_dir = args.flags.get("pdfium").cloned().unwrap_or_else(|| "bin".into());
    let db = db_path(args)?;
    println!("\nIndex: {}\nKiwix: {kiwix_url}\n", db.display());

    // Extra folders: --extra <dir> may be given more than once.
    let extras: Vec<PathBuf> = std::env::args()
        .collect::<Vec<_>>()
        .windows(2)
        .filter(|w| w[0] == "--extra")
        .map(|w| PathBuf::from(&w[1]))
        .collect();
    let opts = museion_core::IndexOptions {
        library: Path::new(&library),
        extra_dirs: &extras,
        db: &db,
        kiwix_url: &kiwix_url,
        pdfium_dir: Path::new(&pdfium_dir),
        force: args.flags.contains_key("force"),
    };
    let s = museion_core::index_library(&opts, |p| {
        use museion_core::Progress::*;
        match p {
            Skipped { book } => println!("  skip     {book}  (already indexed; --force to rebuild)"),
            Native { book } => println!("  native   {book}  (its own full-text index works; nothing to build)"),
            Archive { book, documents } => println!("  building {book}  ({documents} documents)"),
            Document { title, outcome, .. } => match outcome {
                Outcome::ImageOnly => println!("      scan   {title}  (no text layer)"),
                Outcome::Duplicate => println!("      dup    {title}"),
                Outcome::Failed => eprintln!("      FAIL   {title}"),
                Outcome::Indexed => {}
            },
            ArchiveDone { indexed, duplicate, image_only, failed, .. } => println!(
                "           {indexed} indexed, {duplicate} duplicate, {image_only} image-only, {failed} failed"
            ),
        }
    })?;

    println!(
        "\n{} documents, {} pages, {} words indexed ({} image-only skipped)\n",
        s.documents, s.pages, thousands(s.words), s.image_only
    );
    Ok(())
}

/// Compute the GPU offload split for a model on this machine.
///
///   museion fit --model <gguf> --fit-exe <path-to-llama-fit-params> [--ctx 32768]
fn cmd_fit(args: &Args) -> Result<()> {
    let model = args.flags.get("model").context("--model <gguf> is required")?;
    let exe = args
        .flags
        .get("fit-exe")
        .cloned()
        .unwrap_or_else(|| if cfg!(windows) { "bin/llama-fit-params.exe".into() } else { "bin/llama-fit-params".into() });
    let ctx: u32 = args.flags.get("ctx").and_then(|c| c.parse().ok()).unwrap_or(32768);

    println!("\nMeasuring {model} against this machine at {ctx} context...");
    let fit = museion_core::fit_model(Path::new(&exe), Path::new(model), ctx)?;
    println!(
        "\n  GPU layers : {}",
        fit.gpu_layers.map(|n| n.to_string()).unwrap_or_else(|| "all".into())
    );
    println!("  Arguments  : {}\n", fit.args.join(" "));
    Ok(())
}

/// Fetch and unpack the helper programs for this machine.
///
///   museion tools --dest ./bin        (picks CUDA / Vulkan / Metal / CPU)
///   museion tools --dest ./bin --cpu  (force the CPU build)
fn cmd_tools(args: &Args) -> Result<()> {
    use museion_core::tools::{self, Step};
    let dest = args.flags.get("dest").context("--dest <dir> is required")?;
    let accel = if args.flags.contains_key("cpu") {
        museion_core::Accelerator::Cpu
    } else {
        museion_core::detect_hardware()?.accelerator
    };
    println!("
Into: {dest}");
    for (name, key, size) in tools::plan(&accel)? {
        println!("  {name:<14} {key:<16} {}", thousands((size / 1_048_576) as usize) + " MB");
    }
    println!();
    let installed = tools::install(&accel, Path::new(dest), |s| match s {
        Step::Downloading { file, received, total, .. } => {
            if std::io::stdout().flush().is_ok() {
                print!("\r  fetching {file:<44} {:>3}%", if total > 0 { received * 100 / total } else { 0 });
            }
        }
        Step::Unpacking { tool } => println!("\r  unpacking {tool:<50}"),
        Step::Done { tool } => println!("  ok        {tool}"),
        Step::Skipped { tool, reason } => println!("  --        {tool}: {reason}"),
    })?;
    println!(
        "
  kiwix-serve  : {}
  llama-server : {}
  pdfium       : {}
",
        installed.kiwix_serve.map_or("missing".into(), |p| p.display().to_string()),
        installed.llama_server.map_or("missing".into(), |p| p.display().to_string()),
        installed.pdfium_dir.display()
    );
    Ok(())
}


/// Which archives in the library are served by kiwix-serve's own index
/// rather than the local one, so retrieval can search them too.
fn native_books(index: &Index, kiwix: &Llama, args: &Args) -> (Option<museion_core::Kiwix>, Vec<String>) {
    let _ = kiwix;
    let url = args.flags.get("kiwix").cloned().unwrap_or_else(|| "http://127.0.0.1:8090".into());
    let k = museion_core::Kiwix::new(&url);
    if !k.is_up() {
        return (None, Vec::new());
    }
    let processed = index.processed_books().unwrap_or_default();
    let library = args.flags.get("library").cloned();
    let mut books = Vec::new();
    if let Some(dir) = library.or_else(|| args.flags.get("db").and_then(|d| Path::new(d).parent().map(|p| p.display().to_string()))) {
        if let Ok(rd) = std::fs::read_dir(&dir) {
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().to_string();
                if let Some(stem) = name.strip_suffix(".zim") {
                    if !processed.iter().any(|b| b == stem) {
                        books.push(stem.to_string());
                    }
                }
            }
        }
    }
    (Some(k), books)
}

fn cmd_search(args: &Args) -> Result<()> {
    let query = args.rest.join(" ");
    if query.trim().is_empty() {
        bail!("nothing to search for");
    }
    let limit: usize = args
        .flags
        .get("limit")
        .and_then(|s| s.parse().ok())
        .unwrap_or(10);

    let index = Index::open(&db_path(args)?)?;
    let hits = index.search(&query, limit)?;

    if hits.is_empty() {
        println!("\nNothing in the library matches that.\n");
        return Ok(());
    }

    println!();
    for (i, h) in hits.iter().enumerate() {
        let who = if h.author.is_empty() {
            String::new()
        } else {
            format!("  -- {}", h.author)
        };
        println!("{:>2}. {}{}", i + 1, h.doc_title, who);
        println!("    page {} of {}", h.page, h.book);
        println!("    {}", truncate_to(&h.snippet, 150));
        println!();
    }
    Ok(())
}

/// Retrieval plus reranking, without generating an answer.
///
/// This is the stage worth regression-testing: it is where a question either
/// finds the right passages or does not, and it runs without loading a chat
/// model, so the evaluation set is cheap enough to run on every change.
fn cmd_gather(args: &Args) -> Result<()> {
    let question = args.rest.join(" ");
    if question.trim().is_empty() {
        bail!("gather what?");
    }
    let index = Index::open(&db_path(args)?)?;

    let rerank_url = args
        .flags
        .get("reranker")
        .cloned()
        .unwrap_or_else(|| "http://127.0.0.1:8081".into());
    let reranker = Llama::new(&rerank_url);
    let use_rerank = !args.flags.contains_key("no-rerank") && reranker.is_up();

    // A chat model is not needed to gather, but Asker wants one; it is never
    // called on this path.
    let chat = Llama::new("http://127.0.0.1:8080");
    let (kiwix, native) = native_books(&index, &chat, args);
    let asker = Asker {
        index: &index,
        chat: &chat,
        reranker: if use_rerank { Some(&reranker) } else { None },
        kiwix: kiwix.as_ref(),
        native_books: native,
        tuning: tuning(args),
    };

    let (hits, reranked) = asker.gather(&question)?;
    if hits.is_empty() {
        println!("\nNothing in the library covers that.\n");
        return Ok(());
    }

    println!(
        "\n{} passage(s){}\n",
        hits.len(),
        if reranked { ", reranked" } else { ", keyword ranking only" }
    );
    for (i, h) in hits.iter().enumerate() {
        if h.page > 0 {
            println!("{:>2}. {} -- page {}", i + 1, h.doc_title, h.page);
        } else {
            println!("{:>2}. {} -- {}", i + 1, h.doc_title, h.book);
        }
        println!("    {}", truncate_to(&h.body, 220));
        println!();
    }
    Ok(())
}

fn cmd_ask(args: &Args) -> Result<()> {
    let question = args.rest.join(" ");
    if question.trim().is_empty() {
        bail!("ask what?");
    }
    answer_one(args, &question, &[]).map(|_| ())
}

/// A conversation at the terminal: each question is asked with the ones
/// before it, so follow-ups can be tried without the window. An empty line,
/// "quit" or end of input ends it.
///
///   museion chat --db <file>
fn cmd_chat(args: &Args) -> Result<()> {
    use std::io::BufRead;
    let mut history: Vec<museion_core::Exchange> = Vec::new();
    println!("Hypatia. Ask, and ask again; an empty line ends it.\n");
    let stdin = std::io::stdin();
    loop {
        print!("> ");
        let _ = std::io::stdout().flush();
        let mut line = String::new();
        if stdin.lock().read_line(&mut line)? == 0 {
            break;
        }
        let question = line.trim().to_string();
        if question.is_empty() || question.eq_ignore_ascii_case("quit") || question.eq_ignore_ascii_case("exit") {
            break;
        }
        match answer_one(args, &question, &history) {
            Ok(answer) => history.push(museion_core::Exchange { question, answer: answer.text }),
            Err(e) => eprintln!("\nerror: {e:#}\n"),
        }
    }
    Ok(())
}

/// Prepare a drive: see docs/portable.md.
///
///   museion prepare --dest E:\ --bundle go-bag --models qwen3-4b-instruct-q4 --accelerators cuda,vulkan,cpu
fn cmd_prepare(args: &Args) -> Result<()> {
    use museion_core::prepare::{self, Plan, Progress};
    let dest = args.flags.get("dest").map(PathBuf::from).ok_or_else(|| anyhow::anyhow!("need --dest <dir>"))?;
    let bundle = args.flags.get("bundle").cloned().unwrap_or_else(|| "go-bag".into());
    let list = |k: &str, default: &str| -> Vec<String> {
        args.flags
            .get(k)
            .map(|s| s.as_str())
            .unwrap_or(default)
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect()
    };
    let default_accels = if cfg!(target_os = "macos") { "metal" } else { "cuda,vulkan,cpu" };
    let plan = Plan {
        dest,
        bundle,
        models: if args.flags.contains_key("skeleton") { Vec::new() } else { list("models", "qwen3-4b-instruct-q4") },
        accelerators: list("accelerators", default_accels),
        app_exe: args.flags.get("app").map(PathBuf::from),
        installer: args.flags.get("installer").map(PathBuf::from),
        build_index: !args.flags.contains_key("no-index"),
        webview2: !args.flags.contains_key("no-webview2"),
        skeleton: args.flags.contains_key("skeleton"),
    };
    let est = prepare::estimate(&plan)?;
    println!("\nPreparing {}\n", plan.dest.display());
    for (name, bytes) in &est.items {
        println!("  {:>8}  {}", human_gb(*bytes), name);
    }
    println!("\n  {} in all, {} still to fetch, {} free\n", human_gb(est.bytes), human_gb(est.remaining), human_gb(est.free_bytes));
    let mut last = String::new();
    let root = prepare::run(&plan, |p| match p {
        Progress::Phase(s) => println!("\n== {s}"),
        Progress::Note(s) => println!("   {s}"),
        Progress::Download { item, received, total } => {
            let line = if total > 0 {
                format!("   {item}: {}%", received * 100 / total)
            } else {
                format!("   {item}: {}", human_gb(received))
            };
            if line != last {
                print!("\r{line}          ");
                let _ = std::io::stdout().flush();
                last = line;
            }
        }
    })?;
    println!("\nDrive ready at {}\n", root.display());
    Ok(())
}

fn human_gb(bytes: u64) -> String {
    let g = bytes as f64 / 1_073_741_824.0;
    if g >= 1.0 {
        format!("{g:.1} GB")
    } else {
        format!("{} MB", bytes / 1_048_576)
    }
}

/// The prompts and limits: the defaults, or `--tuning <file>` over them.
fn tuning(args: &Args) -> museion_core::Tuning {
    museion_core::Tuning::load(args.flags.get("tuning").map(Path::new))
}

fn answer_one(args: &Args, question: &str, history: &[museion_core::Exchange]) -> Result<museion_core::Answer> {
    let index = Index::open(&db_path(args)?)?;
    if index.stats()?.documents == 0 {
        bail!(
            "the index is empty, so there is nothing to answer from.\n\n  \
             Build it first:  museion index --library <dir>"
        );
    }

    let chat_url = args
        .flags
        .get("llama")
        .cloned()
        .unwrap_or_else(|| "http://127.0.0.1:8080".into());
    let chat = Llama::new(&chat_url);
    if !museion_core::calc::looks_like_arithmetic(&question) && !chat.is_up() {
        bail!(
            "no chat model at {chat_url}\n\n  \
             Start llama-server with a chat model, then try again."
        );
    }

    let rerank_url = args
        .flags
        .get("reranker")
        .cloned()
        .unwrap_or_else(|| "http://127.0.0.1:8081".into());
    let reranker = Llama::new(&rerank_url);
    let use_rerank = !args.flags.contains_key("no-rerank") && reranker.is_up();
    if !use_rerank && !args.flags.contains_key("no-rerank") {
        eprintln!("  (no reranker at {rerank_url}; falling back to keyword ranking)");
    }

    let (kiwix, native) = native_books(&index, &chat, args);
    let asker = Asker {
        index: &index,
        chat: &chat,
        reranker: if use_rerank { Some(&reranker) } else { None },
        kiwix: kiwix.as_ref(),
        native_books: native,
        tuning: tuning(args),
    };

    println!();
    let mut streamed = false;
    let mut shown = String::new();
    let answer = asker.ask(question, history, |t| match t {
        museion_core::Token::Text(s) => {
            streamed = true;
            shown.push_str(&s);
            print!("{s}");
            let _ = std::io::stdout().flush();
        }
        // A terminal cannot unprint; mark where the model started over.
        museion_core::Token::Discard => {
            if streamed {
                println!("
  -- consulted a tool; the answer restarts below --
");
                streamed = false;
            }
            shown.clear();
        }
    })?;
    // Nothing streams when retrieval found nothing, and that answer -- the
    // library saying it does not cover this -- is the one that must not be
    // swallowed.
    if !streamed {
        print!("{}", answer.text);
    }
    println!("\n");

    println!(
        "Basis: {}",
        match answer.basis {
            museion_core::Basis::Library => "from the library",
            museion_core::Basis::LibraryAndCalculation => "from the library, with calculation",
            museion_core::Basis::Computed => "computed",
            museion_core::Basis::GeneralKnowledge => "GENERAL KNOWLEDGE -- not from the library, unverified",
        }
    );
    if !answer.calculations.is_empty() {
        println!("Calculations performed (ground truth):");
        for c in &answer.calculations {
            println!("  {} = {}", c.expression, c.result);
        }
    }
    if !answer.sources.is_empty() {
        println!("Sources{}:", if answer.reranked { " (reranked)" } else { "" });
        // The answer as streamed carries the model's own numbers ([5]), not
        // the renumbered ones in answer.text; the list must use the numbers
        // the reader saw, or [5] above names nothing below.
        let cited = if streamed { museion_core::ask::cited_numbers(&shown) } else { Vec::new() };
        let numbers: Vec<usize> = if cited.len() == answer.sources.len() {
            cited
        } else {
            (1..=answer.sources.len()).collect()
        };
        for (&n, h) in numbers.iter().zip(&answer.sources) {
            let who = if h.author.is_empty() {
                String::new()
            } else {
                format!(", {}", h.author)
            };
            if h.page > 0 {
                println!("  [{}] {}{} -- page {}", n, h.doc_title, who, h.page);
            } else {
                println!("  [{}] {}{} -- {}", n, h.doc_title, who, h.book);
            }
        }
        println!();
    }
    Ok(answer)
}

/// Run the benchmark against the running servers and save the run.
///
///   museion eval --db <file> --label "qwen3.6-35b q4, reranker"
///   museion eval --db <file> --only calculate
fn cmd_eval(args: &Args) -> Result<()> {
    use museion_core::evals::{self, Progress};
    let index = Index::open(&db_path(args)?)?;
    let chat_url = args.flags.get("llama").cloned().unwrap_or_else(|| "http://127.0.0.1:8080".into());
    let chat = Llama::new(&chat_url);
    if !chat.is_up() {
        bail!("no chat model at {chat_url}");
    }
    let rerank_url = args.flags.get("reranker").cloned().unwrap_or_else(|| "http://127.0.0.1:8081".into());
    let reranker = Llama::new(&rerank_url);
    let use_rerank = !args.flags.contains_key("no-rerank") && reranker.is_up();
    let (kiwix, native) = native_books(&index, &chat, args);
    let asker = Asker {
        index: &index,
        chat: &chat,
        reranker: if use_rerank { Some(&reranker) } else { None },
        kiwix: kiwix.as_ref(),
        native_books: native,
        tuning: tuning(args),
    };

    let mut cases = evals::load_cases(evals::BUILTIN)?;
    if let Some(only) = args.flags.get("only") {
        cases.retain(|c| c.capability == *only || c.horizon == *only || c.id == *only);
        if cases.is_empty() {
            bail!("--only {only:?} matches no case, capability or horizon");
        }
    }
    let label = args.flags.get("label").cloned().unwrap_or_else(|| "cli".into());

    println!("
Running {} case(s) against {}{}
", cases.len(), chat.model_id().unwrap_or_default(),
        if use_rerank { " with reranker" } else { " (no reranker)" });
    let run = evals::run(&asker, &cases, &label, |p| match p {
        Progress::Case { index, total, id } => {
            print!("  [{index:>2}/{total}] {id:<22} ");
            let _ = std::io::stdout().flush();
        }
        Progress::Done { passed, .. } => println!("{}", if passed { "pass" } else { "FAIL" }),
    })?;

    println!("
{}/{} passed in {:.0}s
", run.passed, run.total, run.seconds);
    println!("By capability:");
    for (k, p, t) in &run.by_capability {
        println!("  {k:<18} {p}/{t}");
    }
    println!("By horizon:");
    for (k, p, t) in &run.by_horizon {
        println!("  {k:<18} {p}/{t}");
    }
    let failed: Vec<&museion_core::evals::CaseResult> = run.cases.iter().filter(|c| !c.passed).collect();
    if !failed.is_empty() {
        println!("
Failures:");
        for c in failed {
            for ch in c.checks.iter().filter(|ch| !ch.passed) {
                println!("  {:<22} {:<16} {}", c.id, ch.check, truncate_to(&ch.detail, 90));
            }
        }
    }
    let dir = db_path(args)?.parent().map(|p| p.join("evals")).context("db has no parent")?;
    let path = evals::save_run(&dir, &run)?;
    println!("
Saved {}
", path.display());
    Ok(())
}

fn cmd_hardware() -> Result<()> {
    let hw = museion_core::detect_hardware()?;
    let (label, mem) = match &hw.accelerator {
        museion_core::Accelerator::Cuda { name, vram_mb } => (format!("{name} (CUDA)"), *vram_mb),
        museion_core::Accelerator::Metal { name, unified_mb } => (format!("{name} (Metal)"), *unified_mb),
        museion_core::Accelerator::Vulkan { name, vram_mb } => (format!("{name} (Vulkan)"), *vram_mb),
        museion_core::Accelerator::Cpu => ("no usable GPU".to_string(), 0),
    };
    println!("
  Accelerator : {label}");
    if mem > 0 {
        println!("  Memory      : {} MB", thousands(mem as usize));
    }
    println!("  System RAM  : {} MB", thousands(hw.system_ram_mb as usize));
    println!("  Tier        : {:?} -- {}", hw.tier, hw.tier.describe());
    println!();
    Ok(())
}

fn cmd_status(args: &Args) -> Result<()> {
    let index = Index::open(&db_path(args)?)?;
    let s = index.stats()?;
    println!(
        "\n{} documents, {} pages, {} words",
        s.documents,
        s.pages,
        thousands(s.words)
    );
    if s.image_only > 0 {
        println!(
            "{} documents are image-only and hold no searchable text",
            s.image_only
        );
    }
    println!();
    for (book, n) in index.books()? {
        println!("  {book:<40} {n} documents");
    }
    println!();
    Ok(())
}

fn thousands(n: usize) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn truncate_to(s: &str, n: usize) -> String {
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= n {
        flat
    } else {
        format!("{}...", flat.chars().take(n).collect::<String>())
    }
}
