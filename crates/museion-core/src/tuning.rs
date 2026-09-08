//! The prompts and limits of the ask loop, as data.
//!
//! They ship inside the binary as `tuning.yaml` and can be overridden, field
//! by field, by a file of the same name in the app's config folder. That file
//! is read on every question, so editing a prompt and seeing the new behaviour
//! takes as long as saving the file -- no build, no restart, no model reload.

use serde::{Deserialize, Serialize};
use std::path::Path;

/// The defaults, verbatim. Also what "Create override file" writes out, so
/// the person editing it starts from the real text with its comments.
pub const DEFAULT_YAML: &str = include_str!("../tuning.yaml");

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Tuning {
    pub system_prompt: String,
    pub rewrite_prompt: String,
    pub answer_tokens: u32,
    pub max_rounds: usize,
    pub max_continuations: usize,
    pub max_history: usize,
    pub history_chars: usize,
    pub keep: usize,
    pub min_rerank_score: f64,
}

/// An override file may set any subset of the fields.
#[derive(Debug, Default, Deserialize)]
#[serde(default)]
struct Overrides {
    system_prompt: Option<String>,
    rewrite_prompt: Option<String>,
    answer_tokens: Option<u32>,
    max_rounds: Option<usize>,
    max_continuations: Option<usize>,
    max_history: Option<usize>,
    history_chars: Option<usize>,
    keep: Option<usize>,
    min_rerank_score: Option<f64>,
}

impl Default for Tuning {
    fn default() -> Self {
        serde_yaml::from_str(DEFAULT_YAML).expect("the built-in tuning.yaml is valid")
    }
}

impl Tuning {
    /// The defaults with the override file applied on top. No file means the
    /// defaults. A file that will not parse is reported on stderr and ignored:
    /// a typo in the override must not turn into an empty system prompt.
    pub fn load(path: Option<&Path>) -> Self {
        let mut t = Self::default();
        let Some(path) = path else { return t };
        let Ok(text) = std::fs::read_to_string(path) else { return t };
        match serde_yaml::from_str::<Overrides>(text.trim_start_matches('\u{feff}')) {
            Ok(o) => t.apply(o),
            Err(e) => eprintln!("  (ignoring {}: {e})", path.display()),
        }
        t
    }

    fn apply(&mut self, o: Overrides) {
        if let Some(v) = o.system_prompt {
            self.system_prompt = v;
        }
        if let Some(v) = o.rewrite_prompt {
            self.rewrite_prompt = v;
        }
        if let Some(v) = o.answer_tokens {
            self.answer_tokens = v;
        }
        if let Some(v) = o.max_rounds {
            self.max_rounds = v;
        }
        if let Some(v) = o.max_continuations {
            self.max_continuations = v;
        }
        if let Some(v) = o.max_history {
            self.max_history = v;
        }
        if let Some(v) = o.history_chars {
            self.history_chars = v;
        }
        if let Some(v) = o.keep {
            self.keep = v;
        }
        if let Some(v) = o.min_rerank_score {
            self.min_rerank_score = v;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_built_in_defaults_parse_and_keep_their_safety_clauses() {
        let t = Tuning::default();
        assert!(t.system_prompt.contains("do not improvise"));
        assert!(t.system_prompt.contains("Never do arithmetic or unit conversion yourself"));
        assert!(t.system_prompt.contains("EXACTLY as\ncalculate returned it") || t.system_prompt.contains("EXACTLY as calculate returned it"));
        assert!(t.system_prompt.contains("The library has nothing on this, so from general knowledge:"));
        assert!(t.system_prompt.contains("Cite\neach with its bracketed number") || t.system_prompt.contains("Cite each with its bracketed number"));
        assert_eq!(t.keep, 8);
        assert!(t.answer_tokens >= 1000);
    }

    #[test]
    fn an_override_changes_only_what_it_names() {
        let dir = std::env::temp_dir().join(format!("museion-tuning-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("tuning.yaml");
        std::fs::write(&p, "keep: 3\nsystem_prompt: |\n  Be terse. do not improvise.\n").unwrap();
        let t = Tuning::load(Some(&p));
        assert_eq!(t.keep, 3);
        assert_eq!(t.system_prompt.trim(), "Be terse. do not improvise.");
        assert_eq!(t.max_rounds, Tuning::default().max_rounds);

        std::fs::write(&p, "keep: [not a number\n").unwrap();
        let t = Tuning::load(Some(&p));
        assert_eq!(t.keep, 8, "a broken override falls back to the defaults");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
