//! Museion retrieval core.
//!
//! Two retrieval paths, chosen per archive by probing it rather than by
//! guessing from its name:
//!
//!   * archives whose own full-text index works (Wikipedia and the rest of the
//!     mwoffliner catalogue) are queried through kiwix-serve, with nothing to
//!     build and no wait after download;
//!   * archives whose index is unusable -- the zimgit bundles, which are really
//!     collections of PDFs behind a JavaScript viewer -- get a local SQLite
//!     FTS5 index built at import.
//!
//! See docs/retrieval.md for the measurements this design came out of.

pub mod ask;
pub mod index;
pub mod kiwix;
pub mod llama;
pub mod pdf;

pub use ask::{Answer, Asker};
pub use index::{AddOutcome, Hit, Index, IndexStats};
pub use kiwix::{ArchiveKind, CatalogedDoc, Kiwix};
pub use llama::{Llama, Message};
pub use pdf::{Extracted, PdfExtractor};
