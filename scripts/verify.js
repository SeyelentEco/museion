#!/usr/bin/env node
// Re-hash everything in the library against the catalog.
//
// This is the check you run before you need it -- after a long download, after
// moving a drive, and once in a while on a drive that has been sitting in a
// cupboard. Silent bit rot in a file you are relying on is the whole problem.
//
//   node scripts/verify.js --dest E:\museion-library
//   node scripts/verify.js go-bag --dest E:\museion-library

import { join } from 'node:path';
import { stat } from 'node:fs/promises';
import { resolveTarget, loadSources, formatBytes } from './lib/catalog.js';
import { sha256File } from './lib/download.js';
import { parseArgs, resolveLibraryDir, renderProgress, clearProgress, fail } from './lib/cli.js';

async function main() {
  const { positional, flags } = parseArgs(process.argv.slice(2));
  const dest = await resolveLibraryDir(flags);

  const sources = positional.length ? (await resolveTarget(positional[0])).sources : [...(await loadSources()).values()];

  console.log(`\nVerifying ${sources.length} source(s) against ${dest}\n`);

  const missing = [];
  const corrupt = [];
  const ok = [];

  for (const src of sources) {
    const target = join(dest, src.filename);
    let st;
    try {
      st = await stat(target);
    } catch {
      console.log(`  --       ${src.filename}  (not downloaded)`);
      missing.push(src);
      continue;
    }

    if (st.size !== src.size_bytes) {
      console.log(`  BAD      ${src.filename}  size ${st.size}, expected ${src.size_bytes}`);
      corrupt.push(src);
      continue;
    }

    const digest = await sha256File(target, (read) =>
      renderProgress(src.filename, { received: read, total: src.size_bytes, phase: 'hashing' }),
    );
    clearProgress();

    if (digest === src.sha256) {
      console.log(`  ok       ${src.filename}  ${formatBytes(src.size_bytes)}`);
      ok.push(src);
    } else {
      console.log(`  BAD      ${src.filename}  checksum mismatch`);
      console.log(`           expected ${src.sha256}`);
      console.log(`           got      ${digest}`);
      corrupt.push(src);
    }
  }

  console.log(`\n${ok.length} verified, ${corrupt.length} corrupt, ${missing.length} not downloaded\n`);

  if (corrupt.length) {
    console.log('Re-fetch the corrupt files with:');
    for (const s of corrupt) console.log(`  node scripts/fetch.js ${s.id} --dest ${dest} --force`);
    console.log('');
    process.exit(2);
  }
}

main().catch(fail);
