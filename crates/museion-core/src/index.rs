//! The local full-text index.
//!
//! Built only for archives whose own index is unusable. This is keyword
//! indexing over a small curated corpus -- minutes for the Go Bag's 4.7 million
//! words -- not the hours of GPU time that embedding every chunk into a vector
//! database would cost before the tool became usable at all.

use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use std::path::Path;

pub struct Index {
    conn: Connection,
}

#[derive(Debug, Clone)]
pub struct Hit {
    pub doc_title: String,
    pub author: String,
    pub book: String,
    pub file: String,
    pub page: usize,
    pub snippet: String,
    pub rank: f64,
    /// Fraction of the question's content terms this passage actually contains.
    pub coverage: f64,
}

#[derive(Debug, Default)]
pub struct IndexStats {
    pub documents: usize,
    pub pages: usize,
    pub words: usize,
    pub image_only: usize,
    pub duplicates: usize,
}

impl Index {
    pub fn open(path: &Path) -> Result<Self> {
        let conn = Connection::open(path)
            .with_context(|| format!("opening index at {}", path.display()))?;
        conn.execute_batch(
            r#"
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;

            CREATE TABLE IF NOT EXISTS docs (
                id          INTEGER PRIMARY KEY,
                book        TEXT NOT NULL,
                file        TEXT NOT NULL,
                title       TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                author      TEXT NOT NULL DEFAULT '',
                pages       INTEGER NOT NULL DEFAULT 0,
                words       INTEGER NOT NULL DEFAULT 0,
                image_only  INTEGER NOT NULL DEFAULT 0,
                text_sha256 TEXT NOT NULL,
                UNIQUE(book, file)
            );

            -- Content lives here; docs holds the metadata. Page-level rows keep
            -- citations precise: an answer can name the page it came from.
            CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
                title,
                body,
                doc_id UNINDEXED,
                page   UNINDEXED,
                tokenize = 'porter unicode61'
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            "#,
        )
        .context("creating index schema")?;
        Ok(Self { conn })
    }

    /// Has this archive already been indexed?
    pub fn has_book(&self, book: &str) -> Result<bool> {
        let n: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM docs WHERE book = ?1",
            params![book],
            |r| r.get(0),
        )?;
        Ok(n > 0)
    }

    pub fn forget_book(&mut self, book: &str) -> Result<()> {
        let tx = self.conn.transaction()?;
        tx.execute(
            "DELETE FROM pages WHERE doc_id IN (SELECT id FROM docs WHERE book = ?1)",
            params![book],
        )?;
        tx.execute("DELETE FROM docs WHERE book = ?1", params![book])?;
        tx.commit()?;
        Ok(())
    }

    /// True if a document with identical extracted text is already indexed.
    ///
    /// The Go Bag genuinely contains duplicates across archives -- "Where There
    /// Is No Doctor" appears in both the medicine and post-disaster bundles at
    /// the same 182,240 words -- and indexing both would put the same passage
    /// twice into every set of retrieval candidates, crowding out other sources.
    fn duplicate_of(&self, sha: &str) -> Result<Option<String>> {
        Ok(self
            .conn
            .query_row(
                "SELECT title FROM docs WHERE text_sha256 = ?1 LIMIT 1",
                params![sha],
                |r| r.get::<_, String>(0),
            )
            .optional()?)
    }

    #[allow(clippy::too_many_arguments)]
    pub fn add_document(
        &mut self,
        book: &str,
        file: &str,
        title: &str,
        description: &str,
        author: &str,
        extracted: &crate::pdf::Extracted,
    ) -> Result<AddOutcome> {
        let image_only = extracted.is_image_only();

        let mut hasher = Sha256::new();
        for p in &extracted.pages {
            hasher.update(p.text.as_bytes());
        }
        let sha = format!("{:x}", hasher.finalize());

        // Deduplicate only documents that actually yielded text. An image-only
        // scan extracts to zero pages, so every one of them hashes to the hash
        // of nothing and they all collide -- which reported unrelated documents
        // as copies of each other ("foraging guide" as a duplicate of "More
        // Jerky") and hid the scans that genuinely need OCR.
        if !image_only {
            if let Some(existing) = self.duplicate_of(&sha)? {
                return Ok(AddOutcome::Duplicate(existing));
            }
        }
        let tx = self.conn.transaction()?;
        tx.execute(
            "INSERT OR REPLACE INTO docs
               (book, file, title, description, author, pages, words, image_only, text_sha256)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
            params![
                book,
                file,
                title,
                description,
                author,
                extracted.page_count as i64,
                extracted.word_count as i64,
                image_only as i64,
                sha
            ],
        )?;
        let doc_id = tx.last_insert_rowid();

        if !image_only {
            let mut stmt =
                tx.prepare("INSERT INTO pages (title, body, doc_id, page) VALUES (?1,?2,?3,?4)")?;
            for p in &extracted.pages {
                stmt.execute(params![title, p.text, doc_id, p.number as i64])?;
            }
        }
        drop(tx.prepare("SELECT 1")?);
        tx.commit()?;

        Ok(if image_only {
            AddOutcome::ImageOnly
        } else {
            AddOutcome::Indexed
        })
    }

    /// How much of a question's *information* a passage must carry to count as
    /// an answer.
    ///
    /// Terms are OR-ed in the FTS query, because a question's phrasing rarely
    /// matches a document's. The cost is that an OR query always matches
    /// something: "what caused the Bronze Age collapse" pulls up pages on blast
    /// injuries collapsing lungs, and one that literally reads "Ice ages;
    /// Economic collapse". Two terms of three, so a flat ratio lets it through.
    ///
    /// Coverage is therefore weighted by inverse document frequency. Missing
    /// "age" costs almost nothing because nearly every document has it; missing
    /// "bronze" costs almost everything, because that is the term carrying the
    /// question. A passage that skips the distinctive words is not an answer,
    /// however many common ones it happens to contain.
    ///
    /// For a tool consulted precisely when the answer cannot be checked
    /// anywhere else, returning an irrelevant passage confidently is worse than
    /// returning none.
    const MIN_TERM_COVERAGE: f64 = 0.5;

    /// Search the local index. Returns page-level hits ranked by BM25 and
    /// filtered by term coverage.
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<Hit>> {
        let terms = content_terms(query);
        let fts = to_fts_query(query);
        if fts.is_empty() {
            return Ok(Vec::new());
        }
        // The FTS5 match, bm25() and snippet() all happen in a subquery with the
        // table named directly, then docs is joined on the outside. Both
        // alternatives are rejected by SQLite: auxiliary functions cannot see a
        // table alias ("no such column: p"), and naming the table unaliased
        // alongside a JOIN is ambiguous ("ambiguous column name: pages").
        //
        // "rank" is a reserved hidden column on FTS5 tables, so the score is
        // aliased to "score" instead.
        //
        // bm25 weights cover all four columns, including the UNINDEXED ones.
        // Title is weighted below body deliberately: the title is copied onto
        // every page row of a document, so it cannot discriminate between pages
        // of the same document, and over-weighting it floods the results with
        // every page of any topically-related work.
        let mut stmt = self
            .conn
            .prepare(
                r#"
            SELECT d.title, d.author, d.book, d.file, m.page, m.snip, m.score, m.body
            FROM (
                SELECT doc_id, page, body,
                       snippet(pages, 1, '', '', ' ... ', 24) AS snip,
                       bm25(pages, 0.5, 1.0, 0.0, 0.0) AS score
                FROM pages
                WHERE pages MATCH ?1
                ORDER BY score
                LIMIT ?2
            ) m
            JOIN docs d ON d.id = m.doc_id
            ORDER BY m.score
            "#,
            )
            .context("preparing the search query")?;

        // Over-fetch, because coverage filtering removes some of what BM25 ranked.
        let fetch = (limit * 4).max(40) as i64;
        let rows = stmt
            .query_map(params![fts, fetch], |r| {
                Ok((
                    Hit {
                        doc_title: r.get(0)?,
                        author: r.get(1)?,
                        book: r.get(2)?,
                        file: r.get(3)?,
                        page: r.get::<_, i64>(4)? as usize,
                        snippet: r.get(5)?,
                        rank: r.get(6)?,
                        coverage: 0.0,
                    },
                    r.get::<_, String>(7)?,
                ))
            })
            .with_context(|| format!("running search for {fts:?}"))?
            .collect::<Result<Vec<_>, _>>()?;

        let weights = self.term_weights(&terms)?;
        let total: f64 = weights.iter().sum();

        let mut out = Vec::new();
        for (mut hit, body) in rows {
            let haystack = format!("{} {}", hit.doc_title, body).to_lowercase();
            let words: Vec<&str> = haystack
                .split(|c: char| !c.is_alphanumeric())
                .filter(|w| !w.is_empty())
                .collect();

            let mut carried = 0.0;
            for (i, term) in terms.iter().enumerate() {
                // Prefix match at a word boundary, so "purify" finds "purifying"
                // but "age" does not find "storage".
                if words.iter().any(|w| w.starts_with(term.as_str())) {
                    carried += weights[i];
                }
            }

            hit.coverage = if total > 0.0 { carried / total } else { 0.0 };
            if hit.coverage + 1e-9 >= Self::MIN_TERM_COVERAGE {
                out.push(hit);
            }
            if out.len() >= limit {
                break;
            }
        }
        Ok(out)
    }

    /// Inverse document frequency for each term, measured against this index.
    ///
    /// A term the corpus has never seen gets the maximum weight: its absence is
    /// the strongest available evidence that the library does not cover the
    /// question being asked.
    fn term_weights(&self, terms: &[String]) -> Result<Vec<f64>> {
        let total_pages: f64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM pages", [], |r| r.get::<_, i64>(0))?
            as f64;
        let total_pages = total_pages.max(1.0);

        let mut weights = Vec::with_capacity(terms.len());
        for t in terms {
            let df: f64 = self
                .conn
                .query_row(
                    "SELECT COUNT(*) FROM pages WHERE pages MATCH ?1",
                    params![format!("\"{t}\"")],
                    |r| r.get::<_, i64>(0),
                )
                .unwrap_or(0) as f64;
            // Standard smoothed IDF; df of 0 yields the largest weight.
            weights.push(((total_pages + 1.0) / (df + 1.0)).ln().max(0.0));
        }
        Ok(weights)
    }

    pub fn stats(&self) -> Result<IndexStats> {
        let mut s = IndexStats::default();
        self.conn.query_row(
            "SELECT COUNT(*), COALESCE(SUM(pages),0), COALESCE(SUM(words),0),
                    COALESCE(SUM(image_only),0) FROM docs",
            [],
            |r| {
                s.documents = r.get::<_, i64>(0)? as usize;
                s.pages = r.get::<_, i64>(1)? as usize;
                s.words = r.get::<_, i64>(2)? as usize;
                s.image_only = r.get::<_, i64>(3)? as usize;
                Ok(())
            },
        )?;
        Ok(s)
    }

    pub fn books(&self) -> Result<Vec<(String, usize)>> {
        let mut stmt = self
            .conn
            .prepare("SELECT book, COUNT(*) FROM docs GROUP BY book ORDER BY book")?;
        let rows = stmt
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)? as usize))
            })?
            .collect::<Result<Vec<(String, usize)>, _>>()?;
        Ok(rows)
    }
}

pub enum AddOutcome {
    Indexed,
    ImageOnly,
    Duplicate(String),
}

/// Turn a natural-language question into an FTS5 query.
///
/// Everything is quoted and OR-ed. Quoting matters: FTS5 treats bare
/// punctuation and words like NOT and NEAR as syntax, so an unescaped question
/// ("what do I do about a wound that won't close?") is a syntax error rather
/// than a search. OR rather than AND because a question's phrasing rarely
/// matches a document's; BM25 handles the ranking and the reranker fixes the
/// ordering afterwards.
fn to_fts_query(input: &str) -> String {
    content_terms(input)
        .iter()
        .map(|w| format!("\"{w}\""))
        .collect::<Vec<_>>()
        .join(" OR ")
}

/// The words in a question that carry meaning, lowercased and de-duplicated.
fn content_terms(input: &str) -> Vec<String> {
    const STOP: &[&str] = &[
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "do", "does", "did", "how",
        "what", "when", "where", "which", "who", "why", "i", "my", "me", "you", "your", "it", "its",
        "of", "to", "in", "on", "for", "with", "and", "or", "if", "can", "should", "would", "about",
        "from", "that", "this", "there", "without", "any", "some", "not", "no", "get", "make",
        "use", "using", "need", "want", "have", "has",
    ];
    let mut seen = Vec::new();
    for w in input.split(|c: char| !c.is_alphanumeric()) {
        let w = w.trim().to_lowercase();
        if w.len() > 2 && !STOP.contains(&w.as_str()) && !seen.contains(&w) {
            seen.push(w);
        }
    }
    seen
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pdf::{Extracted, Page};

    /// Build a document whose pages are long enough to read as real text.
    /// Padding matters: a page averaging under 20 words is treated as an image
    /// scan, so unpadded fixtures would exercise the wrong branch entirely.
    fn doc(pages: &[&str]) -> Extracted {
        const FILLER: &str = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do \
                              eiusmod tempor incididunt ut labore et dolore magna aliqua";
        let pages: Vec<Page> = pages
            .iter()
            .enumerate()
            .map(|(i, t)| Page {
                number: i + 1,
                text: format!("{t} {FILLER}"),
            })
            .collect();
        let word_count = pages.iter().map(|p| p.text.split_whitespace().count()).sum();
        Extracted {
            page_count: pages.len().max(1),
            pages,
            word_count,
        }
    }

    /// An image-only scan extracts to no pages, so every such document hashes
    /// identically. Treating that as a duplicate marks unrelated scans as
    /// copies of one another and hides the ones that need OCR.
    #[test]
    fn image_only_documents_are_not_duplicates_of_each_other() {
        let dir = std::env::temp_dir().join(format!("museion-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut ix = Index::open(&dir.join("t.db")).unwrap();

        let scan_a = Extracted { pages: vec![], page_count: 40, word_count: 0 };
        let scan_b = Extracted { pages: vec![], page_count: 12, word_count: 0 };

        assert!(matches!(
            ix.add_document("b", "a.pdf", "Foraging Guide", "", "", &scan_a).unwrap(),
            AddOutcome::ImageOnly
        ));
        assert!(matches!(
            ix.add_document("b", "b.pdf", "More Jerky", "", "", &scan_b).unwrap(),
            AddOutcome::ImageOnly
        ));

        let s = ix.stats().unwrap();
        assert_eq!(s.documents, 2, "both scans should be recorded");
        assert_eq!(s.image_only, 2, "both should be reported as needing OCR");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Genuine duplicates do still collapse: the Go Bag really does ship "Where
    /// There Is No Doctor" in two archives at identical length.
    #[test]
    fn identical_text_is_deduplicated() {
        let dir = std::env::temp_dir().join(format!("museion-dup-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut ix = Index::open(&dir.join("t.db")).unwrap();

        let body = doc(&["boil the water for one full minute to make it safe"]);
        assert!(matches!(
            ix.add_document("medicine", "x.pdf", "Where There Is No Doctor", "", "", &body).unwrap(),
            AddOutcome::Indexed
        ));
        match ix.add_document("post-disaster", "y.pdf", "Where-there-is-no-Doctor", "", "", &body).unwrap() {
            AddOutcome::Duplicate(of) => assert_eq!(of, "Where There Is No Doctor"),
            other => panic!("expected a duplicate, got {}", match other {
                AddOutcome::Indexed => "indexed",
                AddOutcome::ImageOnly => "image-only",
                _ => "?",
            }),
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn finds_a_page_by_its_words() {
        let dir = std::env::temp_dir().join(format!("museion-find-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut ix = Index::open(&dir.join("t.db")).unwrap();
        ix.add_document(
            "water",
            "w.pdf",
            "Water Treatment",
            "",
            "WHO",
            &doc(&["unrelated front matter", "add household bleach to disinfect the water"]),
        )
        .unwrap();

        let hits = ix.search("how do I disinfect water with bleach?", 5).unwrap();
        assert!(!hits.is_empty(), "expected a hit");
        assert_eq!(hits[0].page, 2, "should cite the page the text is actually on");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The grounding guarantee: a question whose distinctive term the library
    /// has never seen must return nothing, even when its common words match
    /// plenty of passages. Answering anyway is the failure mode that makes an
    /// offline reference tool dangerous.
    #[test]
    fn refuses_questions_the_library_does_not_cover() {
        let dir = std::env::temp_dir().join(format!("museion-neg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut ix = Index::open(&dir.join("t.db")).unwrap();

        ix.add_document(
            "post-disaster",
            "a.pdf",
            "Survival Manual",
            "",
            "",
            &doc(&[
                "blast overpressure can collapse lungs and rupture eardrums causing injury",
                "economic collapse and ice ages are among the scenarios people prepare for",
            ]),
        )
        .unwrap();

        // "collapse" and "age" both occur; "bronze" never does.
        let hits = ix.search("what caused the Bronze Age collapse", 5).unwrap();
        assert!(
            hits.is_empty(),
            "expected no answer, got {:?}",
            hits.iter().map(|h| (&h.doc_title, h.coverage)).collect::<Vec<_>>()
        );

        // The same corpus must still answer what it genuinely covers.
        let good = ix.search("what does blast overpressure do to lungs", 5).unwrap();
        assert!(!good.is_empty(), "should still answer in-corpus questions");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn drops_stopwords_and_quotes_terms() {
        assert_eq!(
            to_fts_query("How do I purify water?"),
            "\"purify\" OR \"water\""
        );
    }

    #[test]
    fn survives_punctuation_that_fts5_would_choke_on() {
        // Bare "NOT"/quotes/parens are FTS5 syntax; unquoted they are an error.
        let q = to_fts_query("wound that won't close (deep) NOT healing");
        assert!(q.contains("\"wound\""));
        assert!(!q.contains('('));
        assert!(q.contains("\"not\"") || !q.contains("NOT "));
    }

    #[test]
    fn empty_for_a_question_of_pure_stopwords() {
        assert_eq!(to_fts_query("what is it?"), "");
    }
}
