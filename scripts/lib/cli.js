// Shared CLI plumbing: argument parsing, the library path, progress rendering.

import { stat } from 'node:fs/promises';
import { formatBytes } from './catalog.js';

export function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      if (eq !== -1) flags[a.slice(2, eq)] = a.slice(eq + 1);
      else if (argv[i + 1] && !argv[i + 1].startsWith('--')) flags[a.slice(2)] = argv[++i];
      else flags[a.slice(2)] = true;
    } else {
      positional.push(a);
    }
  }
  return { positional, flags };
}

/**
 * Where the library lives. Museion deliberately has no default: a tool that
 * silently fills someone's C: drive with 50 GB of Wikipedia has done something
 * rude. The path must be stated, once, and it is then remembered by the app.
 */
export async function resolveLibraryDir(flags) {
  const dir = flags.dest ?? process.env.MUSEION_LIBRARY;
  if (!dir || dir === true) {
    throw new Error(
      'No library location given.\n\n' +
        '  Museion has no default download directory on purpose -- content runs\n' +
        '  from under a gigabyte to well over 250 GB, so the destination is\n' +
        '  always yours to choose.\n\n' +
        '  Pass one:      --dest E:\\museion-library\n' +
        '  Or set one:    MUSEION_LIBRARY=E:\\museion-library',
    );
  }
  try {
    const st = await stat(dir);
    if (!st.isDirectory()) throw new Error(`${dir} exists but is not a directory`);
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
    // Created on first write by downloadVerified; nothing to do here.
  }
  return dir;
}

const BAR_WIDTH = 28;

export function renderProgress(label, { received, total, phase }) {
  if (!process.stdout.isTTY) return;
  const frac = total > 0 ? Math.min(received / total, 1) : 0;
  const filled = Math.round(frac * BAR_WIDTH);
  const bar = '#'.repeat(filled) + '-'.repeat(BAR_WIDTH - filled);
  const pct = String(Math.round(frac * 100)).padStart(3);
  const tag = phase === 'hashing' ? 'resuming' : 'fetching';
  const line = `  ${tag} ${label.padEnd(34).slice(0, 34)} [${bar}] ${pct}%  ${formatBytes(received)}`;
  process.stdout.write(`\r${line.padEnd(process.stdout.columns ? process.stdout.columns - 1 : 100).slice(0, 200)}`);
}

export function clearProgress() {
  if (process.stdout.isTTY) process.stdout.write('\r' + ' '.repeat((process.stdout.columns ?? 100) - 1) + '\r');
}

export function fail(err) {
  clearProgress();
  console.error(`\nerror: ${err.message}\n`);
  process.exit(1);
}
