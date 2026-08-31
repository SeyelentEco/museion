//! Talking to the kiwix-serve sidecar.
//!
//! All archive content is read over HTTP rather than through a ZIM library
//! binding. That is not laziness: `zimdump dump` on Windows writes extracted
//! files in text mode, turning every 0x0A into 0x0D 0x0A, which silently
//! corrupts every PDF it touches (a 117,197-byte file comes out at 118,056 and
//! every PDF parser then reports a zlib error that looks exactly like a
//! damaged source document). The HTTP route returns byte-exact data, and
//! Museion already supervises kiwix-serve for the browse UI.

use anyhow::{anyhow, Context, Result};
use regex::Regex;
use std::time::Duration;

pub struct Kiwix {
    base: String,
    agent: ureq::Agent,
}

/// One document inside a PDF-collection archive, as described by its database.js.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogedDoc {
    pub file: String,
    pub title: String,
    pub description: String,
    pub author: String,
}

/// What shape an archive is, which decides how it gets indexed.
#[derive(Debug, Clone, PartialEq)]
pub enum ArchiveKind {
    /// A zimgit-style bundle: a JavaScript viewer wrapped around PDF documents,
    /// described by database.js. Needs a locally built index.
    PdfCollection(Vec<CatalogedDoc>),
    /// An mwoffliner-style archive (Wikipedia and friends) whose own full-text
    /// index works. Queried through kiwix-serve directly, nothing to build.
    FullTextIndexed,
}

impl Kiwix {
    pub fn new(base: &str) -> Self {
        Self {
            base: base.trim_end_matches('/').to_string(),
            agent: ureq::AgentBuilder::new()
                .timeout(Duration::from_secs(180))
                .build(),
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}/{}", self.base, path.trim_start_matches('/'))
    }

    pub fn is_up(&self) -> bool {
        self.agent
            .get(&self.url("catalog/v2/entries"))
            .call()
            .is_ok()
    }

    /// Raw bytes of one entry. `path` is percent-encoded here, not by the caller.
    pub fn content(&self, book: &str, path: &str) -> Result<Vec<u8>> {
        let url = self.url(&format!("content/{}/{}", book, encode_path(path)));
        let res = self
            .agent
            .get(&url)
            .call()
            .with_context(|| format!("fetching {url}"))?;
        let mut buf = Vec::new();
        res.into_reader()
            .read_to_end(&mut buf)
            .with_context(|| format!("reading body of {url}"))?;
        Ok(buf)
    }

    /// Decide how an archive must be indexed by probing it.
    ///
    /// Detection is behavioural rather than name-based so that archives which
    /// do not exist yet are still classified correctly.
    pub fn classify(&self, book: &str) -> Result<ArchiveKind> {
        match self.content(book, "database.js") {
            Ok(raw) => {
                let docs = parse_database_js(&String::from_utf8_lossy(&raw));
                if docs.is_empty() {
                    Ok(ArchiveKind::FullTextIndexed)
                } else {
                    Ok(ArchiveKind::PdfCollection(docs))
                }
            }
            Err(_) => Ok(ArchiveKind::FullTextIndexed),
        }
    }

    /// Full-text search through the archive's own index. Used for archives
    /// classified as FullTextIndexed.
    pub fn search(&self, book: &str, pattern: &str, limit: usize) -> Result<Vec<SearchHit>> {
        let url = self.url(&format!(
            "search?books.name={}&pattern={}&format=xml&pageLength={}",
            encode_component(book),
            encode_component(pattern),
            limit
        ));
        let body = self
            .agent
            .get(&url)
            .call()
            .with_context(|| format!("searching {book}"))?
            .into_string()?;
        Ok(parse_opensearch(&body))
    }
}

#[derive(Debug, Clone)]
pub struct SearchHit {
    pub title: String,
    pub link: String,
    pub snippet: String,
}

fn parse_opensearch(xml: &str) -> Vec<SearchHit> {
    let item_re = Regex::new(r"(?s)<item>(.*?)</item>").unwrap();
    let field = |s: &str, tag: &str| -> String {
        Regex::new(&format!(r"(?s)<{tag}>(.*?)</{tag}>"))
            .ok()
            .and_then(|re| re.captures(s).map(|c| c[1].to_string()))
            .map(|v| strip_tags(&unescape(&v)))
            .unwrap_or_default()
    };
    item_re
        .captures_iter(xml)
        .map(|c| SearchHit {
            title: field(&c[1], "title"),
            link: field(&c[1], "link"),
            snippet: field(&c[1], "description"),
        })
        .collect()
}

/// Pull title/description/author/filename out of a zimgit database.js.
///
/// The file is a JavaScript array literal using single quotes, not JSON, so it
/// is scraped rather than parsed. The PDF filenames inside these archives are
/// useless on their own -- "Emergency Preparedness (1).pdf" through "(108).pdf"
/// -- so this is the only source of real titles.
pub fn parse_database_js(src: &str) -> Vec<CatalogedDoc> {
    let rec_re = Regex::new(r"(?s)\{'_id':.*?\}").unwrap();
    let fp_re = Regex::new(r"'fp':\s*\[([^\]]*)\]").unwrap();
    let quoted = Regex::new(r"'((?:[^'\\]|\\.)*)'").unwrap();

    let field = |rec: &str, name: &str| -> String {
        Regex::new(&format!(r"'{name}':\s*'((?:[^'\\]|\\.)*)'"))
            .ok()
            .and_then(|re| re.captures(rec).map(|c| c[1].to_string()))
            .map(|s| s.replace("\\'", "'").trim().to_string())
            .unwrap_or_default()
    };

    let mut out = Vec::new();
    for rec in rec_re.find_iter(src) {
        let rec = rec.as_str();
        let title = field(rec, "ti");
        let description = field(rec, "dsc");
        let author = field(rec, "aut");
        if let Some(fp) = fp_re.captures(rec) {
            for f in quoted.captures_iter(&fp[1]) {
                let file = f[1].replace("\\'", "'");
                if file.to_lowercase().ends_with(".pdf") {
                    out.push(CatalogedDoc {
                        file,
                        title: title.clone(),
                        description: description.clone(),
                        author: author.clone(),
                    });
                }
            }
        }
    }
    out
}

fn strip_tags(s: &str) -> String {
    Regex::new(r"<[^>]*>")
        .unwrap()
        .replace_all(s, "")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn unescape(s: &str) -> String {
    s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// Percent-encode a path, keeping the separators intact.
fn encode_path(path: &str) -> String {
    path.split('/')
        .map(encode_component)
        .collect::<Vec<_>>()
        .join("/")
}

fn encode_component(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Book id as kiwix-serve exposes it on /content/, derived from the filename.
pub fn book_id_from_filename(filename: &str) -> Result<String> {
    filename
        .strip_suffix(".zim")
        .map(|s| s.to_string())
        .ok_or_else(|| anyhow!("{filename} is not a .zim file"))
}
