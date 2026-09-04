//! Building the local index over the library and any extra content folders.
//!
//! One loop, shared by the command line and the desktop app, reporting
//! progress through a callback so a person can see which of 155 documents it
//! is on rather than staring at a spinner for several minutes.
//!
//! Three kinds of thing are indexed:
//!   * PDF-collection archives (the zimgit bundles), read through kiwix-serve;
//!   * archives with a working index of their own, which need nothing built;
//!   * loose PDFs in a folder someone added, read straight from disk. The
//!     catalogue is a starting point, not a boundary -- a manual a person has
//!     found belongs in the same index as everything else.

use crate::{
    index::AddOutcome,
    kiwix::{book_id_from_filename, ArchiveKind},
    Index, Kiwix, PdfExtractor,
};
use anyhow::{bail, Context, Result};
use std::path::{Path, PathBuf};

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
    /// Further folders of content: .zim archives (served by kiwix-serve, so
    /// they must be among the files it was started with) and loose PDFs.
    pub extra_dirs: &'a [PathBuf],
    pub db: &'a Path,
    pub kiwix_url: &'a str,
    pub pdfium_dir: &'a Path,
    pub force: bool,
}

/// The book id under which a folder's loose PDFs are indexed. Stable across
/// runs, distinct from any archive, and recognisable in a citation.
pub fn folder_book_id(dir: &Path) -> String {
    let name = dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| dir.display().to_string());
    format!("folder:{name}")
}

/// Index every archive and loose PDF in the library and extra folders.
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

    let mut dirs: Vec<PathBuf> = vec![opts.library.to_path_buf()];
    dirs.extend(opts.extra_dirs.iter().cloned());

    let mut any = false;
    for dir in &dirs {
        let mut entries: Vec<String> = match std::fs::read_dir(dir) {
            Ok(rd) => rd
                .filter_map(|e| e.ok())
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect(),
            Err(e) => {
                eprintln!("  (skipping {}: {e})", dir.display());
                continue;
            }
        };
        entries.sort();

        // Archives.
        for filename in entries.iter().filter(|n| n.ends_with(".zim")) {
            any = true;
            let book = book_id_from_filename(filename)?;
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
                            Ok(to_outcome(index.add_document(
                                &book, &d.file, &title, &d.description, &d.author, &extracted,
                            )?))
                        })()
                        .unwrap_or(Outcome::Failed);
                        tally(outcome, &mut ok, &mut dup, &mut img, &mut failed);
                        report(Progress::Document { book: book.clone(), title, done: i + 1, total, outcome });
                    }
                    index.mark_book_done(&book)?;
                    report(Progress::ArchiveDone { book, indexed: ok, duplicate: dup, image_only: img, failed });
                }
            }
        }

        // Loose PDFs: only in extra folders. The library folder holds
        // archives and the index, and a stray PDF there is not content.
        if dir == opts.library {
            continue;
        }
        let pdfs: Vec<&String> = entries
            .iter()
            .filter(|n| n.to_lowercase().ends_with(".pdf"))
            .collect();
        if pdfs.is_empty() {
            continue;
        }
        any = true;
        let book = folder_book_id(dir);
        if index.has_book(&book)? && !opts.force {
            report(Progress::Skipped { book });
            continue;
        }
        if opts.force {
            index.forget_book(&book)?;
        }
        let total = pdfs.len();
        report(Progress::Archive { book: book.clone(), documents: total });
        let (mut ok, mut dup, mut img, mut failed) = (0, 0, 0, 0);
        for (i, name) in pdfs.iter().enumerate() {
            let path = dir.join(name);
            let title = title_from_filename(name);
            let outcome = (|| -> Result<Outcome> {
                let bytes = std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
                let extracted = extractor.extract(&bytes)?;
                // `file` is the full path: it is what opens the document later.
                Ok(to_outcome(index.add_document(
                    &book,
                    &path.display().to_string(),
                    &title,
                    "",
                    "",
                    &extracted,
                )?))
            })()
            .unwrap_or(Outcome::Failed);
            tally(outcome, &mut ok, &mut dup, &mut img, &mut failed);
            report(Progress::Document { book: book.clone(), title, done: i + 1, total, outcome });
        }
        index.mark_book_done(&book)?;
        report(Progress::ArchiveDone { book, indexed: ok, duplicate: dup, image_only: img, failed });
    }

    if !any {
        bail!("no .zim archives or PDFs in {}", dirs.iter().map(|d| d.display().to_string()).collect::<Vec<_>>().join(", "));
    }
    index.stats()
}

fn to_outcome(o: AddOutcome) -> Outcome {
    match o {
        AddOutcome::Indexed => Outcome::Indexed,
        AddOutcome::ImageOnly => Outcome::ImageOnly,
        AddOutcome::Duplicate(_) => Outcome::Duplicate,
    }
}

fn tally(o: Outcome, ok: &mut usize, dup: &mut usize, img: &mut usize, failed: &mut usize) {
    match o {
        Outcome::Indexed => *ok += 1,
        Outcome::Duplicate => *dup += 1,
        Outcome::ImageOnly => *img += 1,
        Outcome::Failed => *failed += 1,
    }
}

/// "Where_There_Is_No_Doctor-2015.pdf" -> "Where There Is No Doctor 2015".
/// A found document has no database.js to name it; the filename is all there is.
pub fn title_from_filename(name: &str) -> String {
    let stem = name.rsplit_once('.').map(|(s, _)| s).unwrap_or(name);
    stem.replace(['_', '-'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn titles_from_filenames_read_like_titles() {
        assert_eq!(title_from_filename("Where_There_Is_No_Doctor-2015.pdf"), "Where There Is No Doctor 2015");
        assert_eq!(title_from_filename("FM 21-76.PDF"), "FM 21 76");
    }

    #[test]
    fn folder_ids_are_stable_and_distinct_from_archives() {
        assert_eq!(folder_book_id(Path::new(r"E:\found\manuals")), "folder:manuals");
        assert!(!folder_book_id(Path::new("/x/manuals")).ends_with(".zim"));
    }
}
