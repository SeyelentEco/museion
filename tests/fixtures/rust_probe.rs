// Prints what the Rust index returns for each question, as JSON, so that
// tests/index_corpus.py can record it in rust-search.json for the Python
// index to be compared against. To build it, make a scratch Cargo project
// outside the repository:
//
//   [package]
//   name = "probe"
//   version = "0.1.0"
//   edition = "2021"
//   [dependencies]
//   museion-core = { path = "<this repository>/crates/museion-core" }
//   serde_json = "1"
//   anyhow = "1"
//   [workspace]
//
// with this file as src/main.rs and the repository's Cargo.lock copied in,
// then `cargo build --offline`.
//
// Usage: probe <index.db> <queries.json>, where queries.json is a list of
// [question, limit] pairs.
use museion_core::Index;
use serde_json::json;
use std::path::Path;

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let ix = Index::open(Path::new(&args[1]))?;
    let queries: Vec<(String, usize)> =
        serde_json::from_str(&std::fs::read_to_string(&args[2])?)?;
    let mut results = Vec::new();
    for (q, limit) in &queries {
        let (hits, best) = ix.search_ungated(q, *limit)?;
        let gated = ix.search(q, *limit)?.len();
        let hits: Vec<_> = hits
            .iter()
            .map(|h| {
                json!({
                    "doc_title": h.doc_title, "author": h.author, "book": h.book,
                    "file": h.file, "page": h.page, "snippet": h.snippet,
                    "rank": h.rank, "coverage": h.coverage, "body": h.body,
                })
            })
            .collect();
        results.push(json!({
            "query": q, "limit": limit, "best": best, "gated": gated, "hits": hits,
        }));
    }
    let s = ix.stats()?;
    let out = json!({
        "stats": {"documents": s.documents, "pages": s.pages, "words": s.words,
                  "image_only": s.image_only},
        "books": ix.books()?,
        "processed_books": ix.processed_books()?,
        "results": results,
    });
    println!("{}", serde_json::to_string_pretty(&out)?);
    Ok(())
}
