//! PDF text extraction, via Chromium's pdfium.
//!
//! Extraction quality is not a detail here -- it is the product. A retrieval
//! system can only find what the extractor could read, so this uses the same
//! engine Chrome does rather than a lighter pure-Rust parser that loses text on
//! multi-column and scanned-with-text-layer documents.

use anyhow::{Context, Result};
use pdfium_render::prelude::*;
use std::path::Path;

pub struct PdfExtractor {
    pdfium: Pdfium,
}

/// One page of extracted text. Pages are the natural chunk: they are what a
/// citation can point at, and at roughly 265 words each in this corpus they are
/// already close to the right size for a retrieval window.
#[derive(Debug, Clone)]
pub struct Page {
    pub number: usize,
    pub text: String,
}

pub struct Extracted {
    pub pages: Vec<Page>,
    pub page_count: usize,
    pub word_count: usize,
}

impl Extracted {
    /// True when the document is essentially images -- a scan with no text
    /// layer. Indexing these adds nothing; they need OCR, which is out of scope
    /// for now, so they are recorded and reported rather than silently dropped.
    pub fn is_image_only(&self) -> bool {
        self.page_count > 0 && (self.word_count as f64 / self.page_count as f64) < 20.0
    }
}

impl PdfExtractor {
    /// `lib_dir` holds pdfium.dll / libpdfium.dylib / libpdfium.so.
    pub fn new(lib_dir: &Path) -> Result<Self> {
        let bindings = Pdfium::bind_to_library(Pdfium::pdfium_platform_library_name_at_path(
            &lib_dir,
        ))
        .with_context(|| {
            format!(
                "loading the pdfium library from {}. Museion downloads this \
                 alongside its other sidecars; run setup if it is missing.",
                lib_dir.display()
            )
        })?;
        Ok(Self {
            pdfium: Pdfium::new(bindings),
        })
    }

    pub fn extract(&self, bytes: &[u8]) -> Result<Extracted> {
        let doc = self
            .pdfium
            .load_pdf_from_byte_slice(bytes, None)
            .context("parsing PDF")?;

        let mut pages = Vec::new();
        let mut word_count = 0;

        for (i, page) in doc.pages().iter().enumerate() {
            let text = page
                .text()
                .map(|t| normalise(&t.all()))
                .unwrap_or_default();
            word_count += text.split_whitespace().count();
            if !text.is_empty() {
                pages.push(Page {
                    number: i + 1,
                    text,
                });
            }
        }

        let page_count = doc.pages().len() as usize;
        Ok(Extracted {
            pages,
            page_count,
            word_count,
        })
    }
}

/// Collapse the whitespace and hyphenation artefacts that PDF extraction leaves
/// behind, so that indexed terms match what a person would actually type.
fn normalise(raw: &str) -> String {
    let dehyphenated = raw.replace("-\r\n", "").replace("-\n", "");
    dehyphenated
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim()
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn joins_words_split_across_lines() {
        assert_eq!(normalise("purifi-\ncation of water"), "purification of water");
    }

    #[test]
    fn collapses_runs_of_whitespace() {
        assert_eq!(normalise("boil   the\t\twater\n\n"), "boil the water");
    }
}
