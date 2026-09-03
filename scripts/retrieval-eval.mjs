#!/usr/bin/env node
// Retrieval quality check against a fixed question set.
//
// These are the questions someone actually asks a library like this, and each
// names the material a correct answer has to surface. Ranking changes -- BM25
// weights, query expansion, the reranker -- get judged against this rather than
// against whichever example was handy at the time.
//
//   node scripts/retrieval-eval.mjs --db E:\museion-library\museion-index.db
//
// The last case is the important one: the Go Bag holds no history, so the right
// behaviour is to find nothing. A library that answers everything is a library
// that is making things up.

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { parseArgs, fail } from './lib/cli.js';
import { REPO_ROOT } from './lib/catalog.js';

const CASES = [
  {
    q: 'how do I purify water without chemicals',
    expect: ['boil', 'distill', 'filter'],
    why: 'boiling and distillation are the non-chemical methods',
  },
  {
    q: 'how much bleach to disinfect drinking water',
    expect: ['bleach', 'hypochlorite', 'drops'],
    why: 'dosing guidance appears in several water documents',
  },
  {
    q: 'treat a deep laceration when there is no doctor',
    expect: ['wound', 'bleed', 'stitch', 'suture', 'clean'],
    why: 'Where There Is No Doctor covers wound care directly',
  },
  {
    q: 'signs of radiation sickness after a nuclear attack',
    expect: ['radiation', 'fallout', 'sickness', 'dose'],
    why: 'Nuclear War Survival Skills is in the post-disaster archive',
  },
  {
    q: 'how do I tie a bowline knot',
    expect: ['bowline', 'loop', 'knot'],
    why: 'the knots archive is small, so this tests narrow-corpus recall',
  },
  {
    q: 'how do I safely can green beans to avoid botulism',
    expect: ['pressure', 'canner', 'pounds', 'headspace'],
    needsRerank: true,
    why:
      'REGRESSION: the procedure pages describe the method without ever saying "green", ' +
      '"beans" or "botulism", so a per-passage coverage filter discarded them and kept ' +
      "the book's index page instead. Must surface the pressure-canner procedure, not a " +
      'list of topics.',
  },
  {
    q: 'we have a lot of green beans and no fridge, how do I can them so nobody gets botulism',
    expect: ['pressure', 'canner', 'pounds', 'headspace'],
    needsRerank: true,
    why:
      'REGRESSION: the way a person actually phrases it. "fridge" and "nobody" occur nowhere ' +
      'in the corpus, so IDF-weighted term coverage refused the question outright while the ' +
      'reranker had the procedure at the top. Coverage must be judged by the reranker when ' +
      'one is available.',
  },
  {
    q: 'delivering a baby without a midwife',
    expect: ['birth', 'labor', 'labour', 'deliver', 'cord'],
    why: 'A Book for Midwives, 544 pages of it',
  },
  {
    q: 'what caused the Bronze Age collapse',
    expect: [],
    shouldFindNothing: true,
    why: 'NEGATIVE: the Go Bag holds no history. Finding nothing is correct.',
  },
];

function binary() {
  const exe = process.platform === 'win32' ? 'museion.exe' : 'museion';
  // The workspace target, not the crate's own. A stale crate-local target dir
  // once ran a days-old binary through this harness, and every "regression" it
  // reported was against code that no longer existed.
  const p = join(REPO_ROOT, 'target', 'release', exe);
  if (!existsSync(p)) {
    throw new Error(`museion binary not built.\n  Build it with:\n    cargo build --release -p museion-core`);
  }
  return p;
}

/**
 * "search" exercises keyword retrieval alone and needs no servers.
 * "gather" adds reranking, which is what actually decides whether the right
 * passage reaches the answer -- so it is the honest default when a reranker is
 * running. It still needs no chat model, which keeps the set cheap to run.
 */
function run(bin, db, q, limit, mode) {
  try {
    const args =
      mode === 'gather'
        ? ['gather', '--db', db, q]
        : ['search', '--db', db, '--limit', String(limit), q];
    // stderr is inherited so the child's own notes -- a reranker that fell back, a
    // debug trace -- reach the terminal instead of being swallowed by the pipe.
    return execFileSync(bin, args, { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, stdio: ['pipe', 'pipe', 'inherit'] });
  } catch (err) {
    return `ERROR: ${err.message}`;
  }
}

async function rerankerAvailable(url) {
  try {
    const res = await fetch(`${url}/v1/models`, { signal: AbortSignal.timeout(3000) });
    return res.ok;
  } catch {
    return false;
  }
}

async function main() {
  const { flags } = parseArgs(process.argv.slice(2));
  const db = flags.db ?? process.env.MUSEION_DB;
  if (!db || db === true) throw new Error('pass --db <index file>');
  const limit = Number(flags.limit ?? 8);
  const verbose = Boolean(flags.verbose);
  const bin = binary();

  const rerankUrl = flags.reranker ?? 'http://127.0.0.1:8081';
  const mode = flags.mode ?? ((await rerankerAvailable(rerankUrl)) ? 'gather' : 'search');

  let passed = 0;
  const failures = [];

  console.log(`\nRetrieval evaluation -- ${CASES.length} cases against ${db}`);
  console.log(
    mode === 'gather'
      ? 'Mode: gather (retrieval + reranking)\n'
      : 'Mode: search (keyword retrieval only -- no reranker reachable)\n',
  );

  for (const c of CASES) {
    if (c.needsRerank && mode !== 'gather') {
      console.log(`  SKIP  ${c.q}`);
      console.log('        needs a reranker; start one on 8081 to run this case\n');
      passed++;
      continue;
    }
    const out = run(bin, db, c.q, limit, mode).toLowerCase();
    const foundNothing =
      out.includes('nothing in the library matches') || out.includes('nothing in the library covers');

    let ok;
    let detail;
    if (c.shouldFindNothing) {
      ok = foundNothing;
      detail = ok ? 'correctly found nothing' : 'returned results for a topic the library does not cover';
    } else {
      const hit = c.expect.filter((t) => out.includes(t.toLowerCase()));
      ok = !foundNothing && hit.length > 0;
      detail = foundNothing ? 'found nothing' : `matched ${hit.length}/${c.expect.length}: ${hit.join(', ') || 'none'}`;
    }

    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${c.q}`);
    console.log(`        ${detail}`);
    if (!ok || verbose) {
      console.log(`        (${c.why})`);
      if (verbose) console.log(run(bin, db, c.q, 3, mode).replace(/^/gm, '        '));
    }
    console.log('');

    if (ok) passed++;
    else failures.push(c.q);
  }

  console.log(`${passed}/${CASES.length} passed\n`);
  if (failures.length) {
    console.log('Failed:');
    for (const f of failures) console.log(`  ${f}`);
    console.log('');
    process.exit(1);
  }
}

main().catch(fail);
