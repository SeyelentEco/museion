//! The benchmark: run the question set, score each answer by machine, keep the
//! run so models and configurations can be compared later.
//!
//! Scoring leans on what the system already reports rather than on judging
//! prose: the basis label, the cited sources, the calculator's log. A model
//! that writes a beautiful paragraph with the wrong bleach dose fails the
//! same way a terse one does.

use crate::{ask::Basis, Asker, Token};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::time::Instant;

/// The benchmark shipped with the repo, compiled in so an installed app has it.
pub const BUILTIN: &str = include_str!("../../../evals/benchmark.yaml");

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NumberCheck {
    pub value: f64,
    #[serde(default)]
    pub tolerance: f64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Case {
    pub id: String,
    pub capability: String,
    pub horizon: String,
    pub prompt: String,
    #[serde(default)]
    pub expect_any: Vec<String>,
    #[serde(default)]
    pub expect_all: Vec<String>,
    #[serde(default)]
    pub expect_basis: Option<String>,
    #[serde(default)]
    pub expect_number: Option<NumberCheck>,
    #[serde(default)]
    pub min_sources: Option<usize>,
    #[serde(default)]
    pub min_steps: Option<usize>,
    #[serde(default)]
    pub source_from: Option<String>,
    #[serde(default)]
    pub must_not_contain: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct Benchmark {
    cases: Vec<Case>,
}

pub fn load_cases(yaml: &str) -> Result<Vec<Case>> {
    let b: Benchmark = serde_yaml::from_str(yaml).context("parsing benchmark")?;
    Ok(b.cases)
}

/// One check's verdict, kept so a person can see *why* a case failed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckResult {
    pub check: String,
    pub passed: bool,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaseResult {
    pub id: String,
    pub capability: String,
    pub horizon: String,
    pub prompt: String,
    pub passed: bool,
    pub checks: Vec<CheckResult>,
    pub answer: String,
    pub basis: String,
    pub sources: Vec<String>,
    pub calculations: Vec<String>,
    pub seconds: f64,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Run {
    pub id: String,
    pub started: String,
    pub model: String,
    pub reranker: bool,
    pub label: String,
    pub cases: Vec<CaseResult>,
    pub passed: usize,
    pub total: usize,
    pub by_capability: Vec<(String, usize, usize)>,
    pub by_horizon: Vec<(String, usize, usize)>,
    pub seconds: f64,
}

#[derive(Debug, Clone)]
pub enum Progress {
    Case { index: usize, total: usize, id: String },
    Done { index: usize, total: usize, id: String, passed: bool },
}

/// Run the benchmark. `label` is a free name for the configuration under test.
pub fn run(
    asker: &Asker,
    cases: &[Case],
    label: &str,
    mut report: impl FnMut(Progress),
) -> Result<Run> {
    let started = Instant::now();
    let model = asker.chat.model_id().unwrap_or_else(|_| "unknown".into());
    let stamp = now_stamp();
    let mut results = Vec::with_capacity(cases.len());

    for (i, case) in cases.iter().enumerate() {
        report(Progress::Case { index: i + 1, total: cases.len(), id: case.id.clone() });
        let t0 = Instant::now();
        let outcome = asker.ask(&case.prompt, |t| {
            // Answers are captured from the return value; streaming is
            // ignored here except to honour a discard.
            let _ = matches!(t, Token::Discard);
        });
        let seconds = t0.elapsed().as_secs_f64();

        let result = match outcome {
            Ok(a) => {
                let basis = basis_name(a.basis).to_string();
                let sources: Vec<String> = a
                    .sources
                    .iter()
                    .map(|h| {
                        if h.page > 0 {
                            format!("{} (p.{}) [{}]", h.doc_title, h.page, h.book)
                        } else {
                            format!("{} [{}]", h.doc_title, h.book)
                        }
                    })
                    .collect();
                let calcs: Vec<String> = a
                    .calculations
                    .iter()
                    .map(|c| format!("{} = {}", c.expression, c.result))
                    .collect();
                let checks = score(case, &a.text, &basis, &sources, &a.calculations);
                let passed = checks.iter().all(|c| c.passed);
                CaseResult {
                    id: case.id.clone(),
                    capability: case.capability.clone(),
                    horizon: case.horizon.clone(),
                    prompt: case.prompt.clone(),
                    passed,
                    checks,
                    answer: a.text,
                    basis,
                    sources,
                    calculations: calcs,
                    seconds,
                    error: None,
                }
            }
            Err(e) => CaseResult {
                id: case.id.clone(),
                capability: case.capability.clone(),
                horizon: case.horizon.clone(),
                prompt: case.prompt.clone(),
                passed: false,
                checks: vec![CheckResult { check: "answered".into(), passed: false, detail: format!("{e:#}") }],
                answer: String::new(),
                basis: String::new(),
                sources: Vec::new(),
                calculations: Vec::new(),
                seconds,
                error: Some(format!("{e:#}")),
            },
        };
        report(Progress::Done { index: i + 1, total: cases.len(), id: case.id.clone(), passed: result.passed });
        results.push(result);
    }

    let passed = results.iter().filter(|r| r.passed).count();
    let by_capability = tally(&results, |r| r.capability.clone());
    let by_horizon = tally(&results, |r| r.horizon.clone());

    Ok(Run {
        id: stamp.clone(),
        started: stamp,
        model,
        reranker: asker.reranker.is_some(),
        label: label.to_string(),
        total: results.len(),
        passed,
        cases: results,
        by_capability,
        by_horizon,
        seconds: started.elapsed().as_secs_f64(),
    })
}

fn tally(results: &[CaseResult], key: impl Fn(&CaseResult) -> String) -> Vec<(String, usize, usize)> {
    let mut out: Vec<(String, usize, usize)> = Vec::new();
    for r in results {
        let k = key(r);
        match out.iter_mut().find(|(n, _, _)| *n == k) {
            Some((_, p, t)) => {
                *t += 1;
                if r.passed {
                    *p += 1;
                }
            }
            None => out.push((k, usize::from(r.passed), 1)),
        }
    }
    out
}

fn basis_name(b: Basis) -> &'static str {
    match b {
        Basis::Library => "library",
        Basis::LibraryAndCalculation => "library_and_calculation",
        Basis::Computed => "computed",
        Basis::GeneralKnowledge => "general_knowledge",
    }
}

/// Apply every check a case declares.
pub fn score(
    case: &Case,
    answer: &str,
    basis: &str,
    sources: &[String],
    calcs: &[crate::ask::Calculation],
) -> Vec<CheckResult> {
    let hay = format!("{answer}\n{}", sources.join("\n")).to_lowercase();
    let mut out = Vec::new();

    if !case.expect_any.is_empty() {
        let hit: Vec<&String> = case.expect_any.iter().filter(|t| hay.contains(&t.to_lowercase())).collect();
        out.push(CheckResult {
            check: "expect_any".into(),
            passed: !hit.is_empty(),
            detail: if hit.is_empty() {
                format!("none of {:?} found", case.expect_any)
            } else {
                format!("found {:?}", hit)
            },
        });
    }
    if !case.expect_all.is_empty() {
        let missing: Vec<&String> = case.expect_all.iter().filter(|t| !hay.contains(&t.to_lowercase())).collect();
        out.push(CheckResult {
            check: "expect_all".into(),
            passed: missing.is_empty(),
            detail: if missing.is_empty() { "all present".into() } else { format!("missing {:?}", missing) },
        });
    }
    if let Some(b) = &case.expect_basis {
        out.push(CheckResult {
            check: "expect_basis".into(),
            passed: basis == b,
            detail: format!("wanted {b}, got {basis}"),
        });
    }
    if let Some(n) = &case.expect_number {
        // The calculator's results are the trustworthy place to look; fall
        // back to numbers in the prose so a pre-checked "17 * 23 = 391" counts.
        let mut found: Vec<f64> = calcs.iter().filter_map(|c| c.result.parse::<f64>().ok()).collect();
        found.extend(numbers_in(answer));
        let ok = found.iter().any(|v| (v - n.value).abs() <= n.tolerance + 1e-9);
        out.push(CheckResult {
            check: "expect_number".into(),
            passed: ok,
            detail: format!("wanted {} ± {}, results {:?}", n.value, n.tolerance, found.iter().take(6).collect::<Vec<_>>()),
        });
    }
    if let Some(n) = case.min_sources {
        out.push(CheckResult {
            check: "min_sources".into(),
            passed: sources.len() >= n,
            detail: format!("{} cited, wanted at least {n}", sources.len()),
        });
    }
    if let Some(n) = case.min_steps {
        let steps = answer
            .lines()
            .filter(|l| {
                let t = l.trim_start();
                t.starts_with("- ") || t.starts_with("* ") || t.starts_with("• ") || t.chars().next().is_some_and(|c| c.is_ascii_digit()) && t.contains('.')
            })
            .count();
        out.push(CheckResult {
            check: "min_steps".into(),
            passed: steps >= n,
            detail: format!("{steps} steps, wanted at least {n}"),
        });
    }
    if let Some(s) = &case.source_from {
        let ok = sources.iter().any(|x| x.to_lowercase().contains(&s.to_lowercase()));
        out.push(CheckResult {
            check: "source_from".into(),
            passed: ok,
            detail: if ok { format!("a source matches {s:?}") } else { format!("no source matches {s:?}") },
        });
    }
    if !case.must_not_contain.is_empty() {
        let bad: Vec<&String> = case.must_not_contain.iter().filter(|t| hay.contains(&t.to_lowercase())).collect();
        out.push(CheckResult {
            check: "must_not_contain".into(),
            passed: bad.is_empty(),
            detail: if bad.is_empty() { "clean".into() } else { format!("found {:?}", bad) },
        });
    }
    if out.is_empty() {
        out.push(CheckResult { check: "answered".into(), passed: !answer.trim().is_empty(), detail: "no checks declared".into() });
    }
    out
}

fn numbers_in(text: &str) -> Vec<f64> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for c in text.chars() {
        if c.is_ascii_digit() || (c == '.' && !cur.is_empty()) {
            cur.push(c);
        } else {
            if c == ',' && !cur.is_empty() {
                continue; // thousands separator
            }
            if !cur.is_empty() {
                if let Ok(v) = cur.trim_end_matches('.').parse::<f64>() {
                    out.push(v);
                }
                cur.clear();
            }
        }
    }
    if let Ok(v) = cur.trim_end_matches('.').parse::<f64>() {
        out.push(v);
    }
    out
}

fn now_stamp() -> String {
    // No chrono: seconds since the epoch is stable, sortable, and enough.
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{secs}")
}

pub fn save_run(dir: &Path, run: &Run) -> Result<std::path::PathBuf> {
    std::fs::create_dir_all(dir)?;
    let path = dir.join(format!("run-{}.json", run.id));
    std::fs::write(&path, serde_json::to_string_pretty(run)?)?;
    Ok(path)
}

pub fn list_runs(dir: &Path) -> Vec<Run> {
    let mut runs: Vec<Run> = std::fs::read_dir(dir)
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
                .filter_map(|e| std::fs::read_to_string(e.path()).ok())
                .filter_map(|s| serde_json::from_str::<Run>(&s).ok())
                .collect()
        })
        .unwrap_or_default();
    runs.sort_by(|a, b| b.id.cmp(&a.id));
    runs
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ask::Calculation;

    #[test]
    fn the_builtin_benchmark_parses_and_is_well_formed() {
        let cases = load_cases(BUILTIN).unwrap();
        assert!(cases.len() >= 20);
        let caps = ["retrieve_pdf", "retrieve_article", "calculate", "reason", "plan", "refuse"];
        let hors = ["month", "six_months", "year", "five_years", "any"];
        for c in &cases {
            assert!(caps.contains(&c.capability.as_str()), "{}: capability {}", c.id, c.capability);
            assert!(hors.contains(&c.horizon.as_str()), "{}: horizon {}", c.id, c.horizon);
        }
        let mut ids: Vec<&str> = cases.iter().map(|c| c.id.as_str()).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), cases.len(), "duplicate case ids");
    }

    #[test]
    fn numbers_come_from_the_calculator_first() {
        let case = Case {
            id: "t".into(), capability: "reason".into(), horizon: "month".into(), prompt: String::new(),
            expect_any: vec![], expect_all: vec![], expect_basis: None,
            expect_number: Some(NumberCheck { value: 67.8, tolerance: 1.0 }),
            min_sources: None, min_steps: None, source_from: None, must_not_contain: vec!["343".into()],
        };
        let calcs = vec![Calculation { expression: "convert(55*0.25,tsp,ml)".into(), result: "67.77".into() }];
        let r = score(&case, "add 67.77 ml", "library_and_calculation", &[], &calcs);
        assert!(r.iter().all(|c| c.passed), "{r:?}");
        let bad = score(&case, "add 343.75 ml", "library_and_calculation", &[], &calcs);
        assert!(bad.iter().any(|c| c.check == "must_not_contain" && !c.passed));
    }

    #[test]
    fn steps_are_counted_from_lists() {
        let case = Case {
            id: "t".into(), capability: "plan".into(), horizon: "month".into(), prompt: String::new(),
            expect_any: vec![], expect_all: vec![], expect_basis: None, expect_number: None,
            min_sources: None, min_steps: Some(3), source_from: None, must_not_contain: vec![],
        };
        let r = score(&case, "Do this:\n1. water\n2. shelter\n3. food\n", "library", &[], &[]);
        assert!(r[0].passed, "{r:?}");
        let r = score(&case, "Just one paragraph.", "library", &[], &[]);
        assert!(!r[0].passed);
    }

    #[test]
    fn prose_numbers_with_thousands_separators() {
        assert_eq!(numbers_in("about 1,622 gallons and 2.5 days"), vec![1622.0, 2.5]);
    }
}
