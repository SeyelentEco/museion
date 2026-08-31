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
    q: 'canning vegetables safely to avoid botulism',
    expect: ['can', 'botulism', 'pressure', 'jar'],
    why: 'food preservation, where getting it wrong is dangerous',
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
  const p = join(REPO_ROOT, 'crates', 'museion-core', 'target', 'release', exe);
  if (!existsSync(p)) {
    throw new Error(`museion binary not built.\n  Build it with:\n    cargo build --release --manifest-path crates/museion-core/Cargo.toml`);
  }
  return p;
}

function search(bin, db, q, limit) {
  try {
    return execFileSync(bin, ['search', '--db', db, '--limit', String(limit), q], {
      encoding: 'utf8',
      maxBuffer: 8 * 1024 * 1024,
    });
  } catch (err) {
    return `ERROR: ${err.message}`;
  }
}

async function main() {
  const { flags } = parseArgs(process.argv.slice(2));
  const db = flags.db ?? process.env.MUSEION_DB;
  if (!db || db === true) throw new Error('pass --db <index file>');
  const limit = Number(flags.limit ?? 8);
  const verbose = Boolean(flags.verbose);
  const bin = binary();

  let passed = 0;
  const failures = [];

  console.log(`\nRetrieval evaluation -- ${CASES.length} cases against ${db}\n`);

  for (const c of CASES) {
    const out = search(bin, db, c.q, limit).toLowerCase();
    const foundNothing = out.includes('nothing in the library matches');

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
      if (verbose) console.log(search(bin, db, c.q, 3).replace(/^/gm, '        '));
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
