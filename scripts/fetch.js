#!/usr/bin/env node
// Download a bundle or a single source into the library, verifying as it goes.
//
//   node scripts/fetch.js go-bag --dest E:\museion-library
//   node scripts/fetch.js zimgit-water --dest E:\museion-library
//   node scripts/fetch.js --list

import { join } from 'node:path';
import { resolveTarget, loadBundles, loadSources, loadModels, urlsFor, formatBytes } from './lib/catalog.js';
import { downloadVerified, isVerified } from './lib/download.js';
import { parseArgs, resolveLibraryDir, renderProgress, clearProgress, fail } from './lib/cli.js';

const USAGE = `
Museion content fetcher

  node scripts/fetch.js <bundle|source> --dest <library-dir>
  node scripts/fetch.js --list

Options
  --dest <dir>   Where to put the library. Required (or set MUSEION_LIBRARY).
  --list         Show available bundles and sources, then exit.
  --force        Re-download even if the file is already present and verified.
`;

async function list() {
  const [bundles, sources] = await Promise.all([loadBundles(), loadSources()]);
  console.log('\nBundles\n');
  for (const b of bundles.values()) {
    const total = b.sources.reduce((n, id) => n + (sources.get(id)?.size_bytes ?? 0), 0);
    console.log(`  ${b.id.padEnd(22)} ${formatBytes(total).padStart(9)}  ${b.title ?? ''}`);
    if (b.description) console.log(`  ${' '.repeat(22)}           ${b.description}`);
  }
  console.log('\nSources\n');
  for (const s of sources.values()) {
    console.log(`  ${s.id.padEnd(34)} ${formatBytes(s.size_bytes).padStart(9)}  ${s.category.padEnd(9)} ${s.title}`);
  }
  const models = await loadModels();
  console.log('\nModels  (run "museion hardware" to see which tier this machine is)\n');
  for (const m of models.values()) {
    const tier = m.tiers?.length ? m.tiers.join('/') : m.role;
    console.log(`  ${m.id.padEnd(34)} ${formatBytes(m.size_bytes).padStart(9)}  ${tier.padEnd(16)} ${m.title}`);
  }
  console.log('');
}

async function main() {
  const { positional, flags } = parseArgs(process.argv.slice(2));

  if (flags.help || flags.h) {
    console.log(USAGE);
    return;
  }
  if (flags.list) return list();
  if (positional.length === 0) {
    console.log(USAGE);
    process.exit(1);
  }

  const dest = await resolveLibraryDir(flags);
  const { bundle, sources } = await resolveTarget(positional[0]);

  const totalBytes = sources.reduce((n, s) => n + s.size_bytes, 0);
  console.log(`\n${bundle ? `Bundle: ${bundle.title ?? bundle.id}` : `Source: ${sources[0].title}`}`);
  console.log(`Into:   ${dest}`);
  console.log(`Size:   ${formatBytes(totalBytes)} across ${sources.length} file(s)\n`);

  let downloaded = 0;
  let skipped = 0;

  for (const src of sources) {
    const target = join(dest, src.filename);

    if (!flags.force) {
      renderProgress(src.filename, { received: 0, total: src.size_bytes, phase: 'hashing' });
      const ok = await isVerified(target, src, (read) =>
        renderProgress(src.filename, { received: read, total: src.size_bytes, phase: 'hashing' }),
      );
      clearProgress();
      if (ok) {
        console.log(`  ok       ${src.filename}  (already present, verified)`);
        skipped++;
        continue;
      }
    }

    try {
      const { url } = await downloadVerified({
        urls: urlsFor(src),
        dest: target,
        size_bytes: src.size_bytes,
        sha256: src.sha256,
        onProgress: (p) => renderProgress(src.filename, p),
      });
      clearProgress();
      const host = new URL(url).host;
      console.log(`  done     ${src.filename}  ${formatBytes(src.size_bytes)}  via ${host}`);
      downloaded++;
    } catch (err) {
      clearProgress();
      console.error(`  FAILED   ${src.filename}\n           ${err.message.replace(/\n/g, '\n           ')}`);
      throw new Error(`${src.id} could not be fetched`);
    }
  }

  console.log(`\n${downloaded} downloaded, ${skipped} already present. Library is at ${dest}\n`);
}

main().catch(fail);
