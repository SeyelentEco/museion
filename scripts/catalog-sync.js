#!/usr/bin/env node
// Build and refresh catalog manifests from the upstream mirror.
//
// Kiwix publishes a .sha256, a .magnet (carrying the BitTorrent infohash and
// the exact byte length) and a .torrent next to every ZIM. So a manifest can
// be produced with complete, authentic integrity data without downloading the
// content itself -- which matters when the content is 115 GB.
//
// Hand-typed hashes rot. Always regenerate.
//
//   node scripts/catalog-sync.js add <zim-url> --category survival
//   node scripts/catalog-sync.js check          # any newer versions upstream?

import { writeFile, readFile } from 'node:fs/promises';
import { join, basename } from 'node:path';
import YAML from 'yaml';
import { CATALOG_DIR, loadSources, CATEGORIES, formatBytes } from './lib/catalog.js';
import { parseArgs, fail } from './lib/cli.js';

const USAGE = `
Museion catalog sync

  node scripts/catalog-sync.js add <zim-url> --category <cat> [--id <id>] [--title "..."]
  node scripts/catalog-sync.js check

Categories: ${CATEGORIES.join(', ')}
`;

async function get(url, { method = 'GET' } = {}) {
  const res = await fetch(url, { method, redirect: 'follow' });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res;
}

/** Pull sha256, byte length and magnet for a ZIM, using its sidecar files. */
async function probe(url) {
  const sha256 = (await (await get(`${url}.sha256`)).text()).trim().split(/\s+/)[0];
  if (!/^[0-9a-f]{64}$/.test(sha256)) {
    throw new Error(`${url}.sha256 did not contain a sha256 (got ${JSON.stringify(sha256.slice(0, 80))})`);
  }

  let magnet = null;
  let size_bytes = null;
  try {
    magnet = (await (await get(`${url}.magnet`)).text()).trim();
    const xl = magnet.match(/[?&]xl=(\d+)/);
    if (xl) size_bytes = Number(xl[1]);
  } catch {
    // Not every mirror carries magnets; fall back to a HEAD below.
  }

  if (size_bytes == null) {
    const head = await get(url, { method: 'HEAD' });
    const len = head.headers.get('content-length');
    if (!len) throw new Error(`could not determine size of ${url}`);
    size_bytes = Number(len);
  }

  let torrent = null;
  try {
    await get(`${url}.torrent`, { method: 'HEAD' });
    torrent = `${url}.torrent`;
  } catch {
    // optional
  }

  return { sha256, size_bytes, magnet, torrent };
}

/**
 * Split a Kiwix filename into its stable part and its release date.
 *
 *   zimgit-post-disaster_en_2024-05.zim          -> zimgit-post-disaster        + 2024-05
 *   wikipedia_en_chemistry_nopic_2026-07.zim     -> wikipedia_en_chemistry_nopic + 2026-07
 *
 * Kiwix names are project_lang_selection_flavour_date, and the number of
 * segments varies, so only the trailing YYYY-MM is parsed. Trying to identify
 * the language segment positionally gets "nopic" wrong and leaks the date into
 * the id, which then breaks staleness checking -- the one thing the date is for.
 */
function parseZimName(filename) {
  const m = filename.match(/^(.+)_(\d{4}-\d{2})\.zim$/);
  if (!m) return { stem: filename.replace(/\.zim$/, ''), date: null };
  return { stem: m[1], date: m[2] };
}

/**
 * Mirrors, harvested from the magnet's webseed (ws=) list rather than guessed.
 *
 * Upstream currently emits those with a doubled scheme -- "https://https://host/..."
 * -- so they are repaired here. Getting this right is the difference between a
 * library that downloads when download.kiwix.org is unreachable and one that
 * does not, which is rather the point of the project.
 */
function mirrorsFor(url, magnet) {
  const path = url.replace(/^https?:\/\/[^/]+/, '');
  const found = [`https://lb.download.kiwix.org${path}`, `https://www.mirrorservice.org/sites/download.kiwix.org${path}`];

  if (magnet) {
    for (const m of magnet.matchAll(/[?&]ws=([^&]+)/g)) {
      const repaired = decodeURIComponent(m[1]).replace(/^https?:\/\/(?=https?:\/\/)/, '');
      try {
        found.push(new URL(repaired).href);
      } catch {
        // Skip anything still malformed rather than poisoning the mirror list.
      }
    }
  }

  const seen = new Set([url]);
  const unique = [];
  for (const u of found) {
    if (seen.has(u)) continue;
    seen.add(u);
    unique.push(u);
  }
  return unique;
}

async function add(url, flags) {
  if (!flags.category || flags.category === true) {
    throw new Error(`--category is required (one of: ${CATEGORIES.join(', ')})`);
  }
  if (!CATEGORIES.includes(flags.category)) {
    throw new Error(`unknown category ${JSON.stringify(flags.category)}; expected one of ${CATEGORIES.join(', ')}`);
  }

  const filename = basename(new URL(url).pathname);
  const { stem, date } = parseZimName(filename);
  const id = flags.id && flags.id !== true ? flags.id : stem;

  process.stdout.write(`probing ${filename} ... `);
  const { sha256, size_bytes, magnet, torrent } = await probe(url);
  console.log(`${formatBytes(size_bytes)}, sha256 ${sha256.slice(0, 12)}...`);

  const doc = {
    id,
    title: flags.title && flags.title !== true ? flags.title : stem,
    category: flags.category,
    filename,
    version: date,
    size_bytes,
    sha256,
    urls: [url],
    mirrors: mirrorsFor(url, magnet),
    ...(torrent ? { torrent } : {}),
    ...(magnet ? { magnet } : {}),
    license: {
      name: flags.license && flags.license !== true ? flags.license : 'CC-BY-SA-4.0',
      attribution: flags.attribution && flags.attribution !== true ? flags.attribution : 'Kiwix and contributors',
    },
  };

  const out = join(CATALOG_DIR, 'sources', `${id}.yaml`);
  const header =
    '# Generated by scripts/catalog-sync.js -- do not hand-edit size_bytes or sha256.\n' +
    `# Regenerate with: node scripts/catalog-sync.js add ${url} --category ${flags.category}\n`;
  await writeFile(out, header + YAML.stringify(doc), 'utf8');
  console.log(`wrote ${out}`);
}

/** Look for newer dated releases of everything already in the catalog. */
async function check() {
  const sources = await loadSources();
  const dirCache = new Map();
  let stale = 0;

  for (const src of sources.values()) {
    const url = src.urls[0];
    const dir = url.slice(0, url.lastIndexOf('/') + 1);
    if (!dirCache.has(dir)) {
      dirCache.set(dir, await (await get(dir)).text());
    }
    const html = dirCache.get(dir);
    const { stem } = parseZimName(src.filename);
    const escaped = stem.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const found = [...html.matchAll(new RegExp(`${escaped}_(\\d{4}-\\d{2})\\.zim(?!\\.)`, 'g'))];
    const latest = found.map((m) => m[1]).sort().at(-1);

    if (latest && src.version && latest > src.version) {
      console.log(`  STALE    ${src.id}  have ${src.version}, upstream has ${latest}`);
      console.log(`           node scripts/catalog-sync.js add ${dir}${stem}_en_${latest}.zim --category ${src.category}`);
      stale++;
    } else if (!latest) {
      console.log(`  GONE?    ${src.id}  no matching file found at ${dir}`);
      stale++;
    } else {
      console.log(`  current  ${src.id}  ${src.version}`);
    }
  }

  console.log(`\n${sources.size - stale} current, ${stale} needing attention\n`);
}

async function main() {
  const { positional, flags } = parseArgs(process.argv.slice(2));
  const cmd = positional[0];

  if (flags.help || !cmd) return console.log(USAGE);
  if (cmd === 'add') {
    if (!positional[1]) throw new Error('add requires a ZIM URL');
    return add(positional[1], flags);
  }
  if (cmd === 'check') return check();

  console.log(USAGE);
  process.exit(1);
}

main().catch(fail);
