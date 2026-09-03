//! Building the local index over a library directory.
//!
//! One loop, shared by the command line and the desktop app, reporting
//! progress through a callback so a person can see which of 155 documents it
//! is on rather than staring at a spinner for several minutes.

use crate::{
    index::AddOutcome,
    kiwix::{book_id_from_filename, ArchiveKind},
    Index, Kiwix, PdfExtractor,
};
use anyhow::{bail, Context, Result};
use std::path::Path;

#[derive(Debug, Clone)]
pub enum Progress {
    /// Starting on an archive that needs a local index built.
    Archive { book: String, documents: usize },
    /// An archive whose own index works; nothing to build.
    Native { book: String },
    /// Already indexed; left alone.
    Skipped { book: String },
    /// One document handled. `done` counts within the current archive.
    Document { book: String, title: String, done: usize, total: usize, outcome: Outcome },
    /// Finished an archive.
    ArchiveDone { book: String, indexed: usize, duplicate: usize, image_only: usize, failed: usize },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Indexed,
    Duplicate,
    ImageOnly,
    Failed,
}

pub struct IndexOptions<'a> {
    pub library: &'a Path,
    pub db: &'a Path,
    pub kiwix_url: &'a str,
    pub pdfium_dir: &'a Path,
    pub force: bool,
}

/// Index every archive in the library. Returns the final stats.
pub fn index_library(
    opts: &IndexOptions,
    mut report: impl FnMut(Progress),
) -> Result<crate::IndexStats> {
    let kiwix = Kiwix::new(opts.kiwix_url);
    if !kiwix.is_up() {
        bail!(
            "no kiwix-serve at {}. Start it over the library first.",
            opts.kiwix_url
        );
    }
    let extractor = PdfExtractor::new(opts.pdfium_dir)?;
    let mut index = Index::open(opts.db)?;

    let mut zims: Vec<String> = std::fs::read_dir(opts.library)
        .with_context(|| format!("reading {}", opts.library.display()))?
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|n| n.ends_with(".zim"))
        .collect();
    zims.sort();
    if zims.is_empty() {
        bail!("no .zim archives in {}", opts.library.display());
    }

    for filename in zims {
        let book = book_id_from_filename(&filename)?;

        if index.has_book(&book)? && !opts.force {
            report(Progress::Skipped { book });
            continue;
        }

        match kiwix.classify(&book)? {
            ArchiveKind::FullTextIndexed => report(Progress::Native { book }),
            ArchiveKind::PdfCollection(docs) => {
                if opts.force {
                    index.forget_book(&book)?;
                }
                let total = docs.len();
                report(Progress::Archive { book: book.clone(), documents: total });
                let (mut ok, mut dup, mut img, mut failed) = (0, 0, 0, 0);

                for (i, d) in docs.iter().enumerate() {
                    let title = if d.title.is_empty() { d.file.clone() } else { d.title.clone() };
                    let outcome = (|| -> Result<Outcome> {
                        let bytes = kiwix.content(&book, &format!("files/{}", d.file))?;
                        let extracted = extractor.extract(&bytes)?;
                        Ok(match index.add_document(
                            &book, &d.file, &title, &d.description, &d.author, &extracted,
                        )? {
                            AddOutcome::Indexed => Outcome::Indexed,
                            AddOutcome::ImageOnly => Outcome::ImageOnly,
                            AddOutcome::Duplicate(_) => Outcome::Duplicate,
                        })
                    })()
                    .unwrap_or(Outcome::Failed);

                    match outcome {
                        Outcome::Indexed => ok += 1,
                        Outcome::Duplicate => dup += 1,
                        Outcome::ImageOnly => img += 1,
                        Outcome::Failed => failed += 1,
                    }
                    report(Progress::Document {
                        book: book.clone(),
                        title,
                        done: i + 1,
                        total,
                        outcome,
                    });
                }
                // Recorded even when every document was a duplicate, so the
                // archive is not re-fetched and re-extracted on the next run.
                index.mark_book_done(&book)?;
                report(Progress::ArchiveDone {
                    book,
                    indexed: ok,
                    duplicate: dup,
                    image_only: img,
                    failed,
                });
            }
        }
    }

    index.stats()
}
