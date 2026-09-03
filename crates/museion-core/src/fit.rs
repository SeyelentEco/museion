//! Computing the GPU offload split for a model on *this* machine.
//!
//! Recent llama.cpp ships `llama-fit-params`, which measures the model against
//! the card actually present and prints ready-to-use `-ngl` and `-ot`
//! arguments. Museion runs it at setup rather than shipping a table, because
//! two people with the same nominal GPU have different amounts free -- a second
//! monitor or an open browser changes the answer -- and a split that is one
//! layer too generous fails to load, while one that is too cautious leaves
//! speed on the table.
//!
//! Measured here: against an RTX 3060 it put 41 layers on the GPU and the
//! expert tensors of blocks 15-40 on the CPU, matching a split that had been
//! reached by hand over several evenings.

use anyhow::{bail, Context, Result};
use std::path::Path;
use std::process::Command;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fit {
    /// Arguments to pass straight to llama-server, e.g. `-ngl 41 -ot "..."`.
    pub args: Vec<String>,
    /// The `-ngl` value on its own, since the supervisor sets it separately.
    pub gpu_layers: Option<u32>,
}

/// Run llama-fit-params and parse what it prints.
///
/// KV cache is measured at q8_0, not q4_0: that is what Museion runs with,
/// because aggressive KV quantisation degrades tool calling, and the fit has to
/// be measured for the configuration that will actually be used.
pub fn fit_model(fit_exe: &Path, model: &Path, ctx: u32) -> Result<Fit> {
    if !fit_exe.exists() {
        bail!(
            "llama-fit-params not found at {}. It ships with llama.cpp builds \
             from 2026 onward; fetch a current build.",
            fit_exe.display()
        );
    }
    let out = Command::new(fit_exe)
        .arg("-m")
        .arg(model)
        .arg("-c")
        .arg(ctx.to_string())
        .args(["-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0"])
        .output()
        .with_context(|| format!("running {}", fit_exe.display()))?;

    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        bail!(
            "llama-fit-params failed ({}):\n{}",
            out.status,
            err.lines().rev().take(6).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join("\n")
        );
    }

    // Everything diagnostic goes to stderr; stdout is the one line we want.
    let stdout = String::from_utf8_lossy(&out.stdout);
    let line = stdout
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with('-'))
        .last()
        .context("llama-fit-params printed no arguments")?;

    let args = split_shell_words(line);
    let gpu_layers = args
        .windows(2)
        .find(|w| w[0] == "-ngl" || w[0] == "--n-gpu-layers")
        .and_then(|w| w[1].parse().ok());

    Ok(Fit { args, gpu_layers })
}

/// Split a command line the way a shell would, honouring double quotes.
///
/// The `-ot` regex is quoted and contains characters like `|` and `(` that
/// must survive intact -- splitting on whitespace alone would break it.
pub fn split_shell_words(line: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_quotes = false;
    let mut had_token = false;

    for c in line.chars() {
        match c {
            '"' => {
                in_quotes = !in_quotes;
                had_token = true;
            }
            c if c.is_whitespace() && !in_quotes => {
                if had_token {
                    out.push(std::mem::take(&mut cur));
                    had_token = false;
                }
            }
            c => {
                cur.push(c);
                had_token = true;
            }
        }
    }
    if had_token {
        out.push(cur);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_the_quoted_regex_as_one_argument() {
        let line = r#"-c 32768 -ngl 41 -ot "blk\.15\.ffn_down.*=CPU,blk\.16\.ffn_(up|down)_exps=CPU""#;
        let args = split_shell_words(line);
        assert_eq!(args.len(), 6);
        assert_eq!(args[4], "-ot");
        assert_eq!(args[5], r"blk\.15\.ffn_down.*=CPU,blk\.16\.ffn_(up|down)_exps=CPU");
    }

    #[test]
    fn empty_and_whitespace_only_lines_yield_nothing() {
        assert!(split_shell_words("").is_empty());
        assert!(split_shell_words("   ").is_empty());
    }
}
