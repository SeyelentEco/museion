//! museion -- index and search the offline library from the command line.
//!
//! The same code the desktop app uses, exposed as a CLI so retrieval can be
//! measured and regression-tested without a GUI in the way.
//!
//!   museion index  --library E:\museion-library --db library.db
//!   museion search --db library.db "how do I purify water"
//!   museion status --db library.db

use anyhow::{bail, Context, Result};
use museion_core::{
    index::AddOutcome,
    kiwix::{book_id_from_filename, ArchiveKind},
    Asker, Index, Kiwix, Llama, PdfExtractor,
};
use std::io::Write;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

const USAGE: &str = "\
museion -- offline library index and search

  museion index  --library <dir> [--db <file>] [--kiwix <url>] [--force]
  museion search --db <file> <query...> [--limit N]
  museion ask    --db <file> <question...> [--llama <url>] [--reranker <url>]
  museion status --db <file>
  museion hardware

Options
  --library <dir>   Directory holding .zim archives
  --db <file>       Index file (default: <library>/museion-index.db)
  --kiwix <url>     kiwix-serve base URL (default: http://127.0.0.1:8090)
  --pdfium <dir>    Directory holding the pdfium library (default: ./bin)
  --llama <url>     chat model server (default: http://127.0.0.1:8080)
  --reranker <url>  reranker server (default: http://127.0.0.1:8081)
  --no-rerank       Skip reranking, use keyword ranking alone
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
        "gather" => cmd_gather(&args),
        "hardware" => cmd_hardware(),
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
    let library = args
        .flags
        .get("library")
        .context("--library is required")?
        .clone();
    let kiwix_url = args
        .flags
        .get("kiwix")
        .cloned()
        .unwrap_or_else(|| "http://127.0.0.1:8090".into());
    let pdfium_dir = args
        .flags
        .get("pdfium")
        .cloned()
        .unwrap_or_else(|| "bin".into());
    let force = args.flags.contains_key("force");

    let kiwix = Kiwix::new(&kiwix_url);
    if !kiwix.is_up() {
        bail!(
            "no kiwix-serve at {kiwix_url}\n\n  \
             Start it with the archives you want indexed, for example:\n  \
             kiwix-serve --port 8090 <library>/*.zim"
        );
    }

    let extractor = PdfExtractor::new(Path::new(&pdfium_dir))?;
    let db = db_path(args)?;
    let mut index = Index::open(&db)?;
    println!("\nIndex: {}\nKiwix: {kiwix_url}\n", db.display());

    let mut zims: Vec<_> = std::fs::read_dir(&library)
        .with_context(|| format!("reading library directory {library}"))?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with(".zim"))
        .collect();
    zims.sort();

    if zims.is_empty() {
        bail!("no .zim archives found in {library}");
    }

    for filename in zims {
        let book = book_id_from_filename(&filename)?;

        if index.has_book(&book)? && !force {
            println!("  skip     {book}  (already indexed; --force to rebuild)");
            continue;
        }

        match kiwix.classify(&book)? {
            ArchiveKind::FullTextIndexed => {
                println!("  native   {book}  (its own full-text index works; nothing to build)");
            }
            ArchiveKind::PdfCollection(docs) => {
                if force {
                    index.forget_book(&book)?;
                }
                println!("  building {book}  ({} documents)", docs.len());
                let (mut ok, mut dup, mut img, mut failed) = (0, 0, 0, 0);

                for d in &docs {
                    let path = format!("files/{}", d.file);
                    let bytes = match kiwix.content(&book, &path) {
                        Ok(b) => b,
                        Err(e) => {
                            eprintln!("      FAIL fetch {}: {}", d.file, truncate(&e.to_string()));
                            failed += 1;
                            continue;
                        }
                    };
                    let extracted = match extractor.extract(&bytes) {
                        Ok(x) => x,
                        Err(e) => {
                            eprintln!("      FAIL parse {}: {}", d.file, truncate(&e.to_string()));
                            failed += 1;
                            continue;
                        }
                    };
                    let title = if d.title.is_empty() {
                        d.file.clone()
                    } else {
                        d.title.clone()
                    };
                    match index.add_document(
                        &book,
                        &d.file,
                        &title,
                        &d.description,
                        &d.author,
                        &extracted,
                    )? {
                        AddOutcome::Indexed => ok += 1,
                        AddOutcome::ImageOnly => {
                            img += 1;
                            println!(
                                "      scan   {title}  ({}p, no text layer)",
                                extracted.page_count
                            );
                        }
                        AddOutcome::Duplicate(of) => {
                            dup += 1;
                            println!("      dup    {title}  (same text as \"{of}\")");
                        }
                    }
                }
                println!(
                    "           {ok} indexed, {dup} duplicate, {img} image-only, {failed} failed"
                );
            }
        }
    }

    let s = index.stats()?;
    println!(
        "\n{} documents, {} pages, {} words indexed ({} image-only skipped)\n",
        s.documents,
        s.pages,
        thousands(s.words),
        s.image_only
    );
    Ok(())
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
    let asker = Asker {
        index: &index,
        chat: &chat,
        reranker: if use_rerank { Some(&reranker) } else { None },
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
        println!("{:>2}. {} -- page {}", i + 1, h.doc_title, h.page);
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
    if !chat.is_up() {
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

    let asker = Asker {
        index: &index,
        chat: &chat,
        reranker: if use_rerank { Some(&reranker) } else { None },
    };

    println!();
    let mut streamed = false;
    let answer = asker.ask(&question, |t| {
        streamed = true;
        print!("{t}");
        let _ = std::io::stdout().flush();
    })?;
    // Nothing streams when retrieval found nothing, and that answer -- the
    // library saying it does not cover this -- is the one that must not be
    // swallowed.
    if !streamed {
        print!("{}", answer.text);
    }
    println!("\n");

    if !answer.sources.is_empty() {
        println!("Sources{}:", if answer.reranked { " (reranked)" } else { "" });
        for (i, h) in answer.sources.iter().enumerate() {
            let who = if h.author.is_empty() {
                String::new()
            } else {
                format!(", {}", h.author)
            };
            println!("  [{}] {}{} -- page {}", i + 1, h.doc_title, who, h.page);
        }
        println!();
    }
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

fn truncate(s: &str) -> String {
    truncate_to(s, 70)
}

fn truncate_to(s: &str, n: usize) -> String {
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= n {
        flat
    } else {
        format!("{}...", flat.chars().take(n).collect::<String>())
    }
}
