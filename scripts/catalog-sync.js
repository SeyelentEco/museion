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

async function get(url, { method = 'GET', headers } = {}) {
  const res = await fetch(url, { method, redirect: 'follow', headers });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res;
}

/**
 * Ask the Kiwix catalogue what an archive actually is.
 *
 * Filenames are not descriptions. "survivors_en_all_maxi" reads, in a
 * preparedness catalogue, exactly like the Survivor Library -- a large
 * collection of pre-industrial technical works. It is in fact a YouTube archive
 * for the video game Vampire Survivors: 13,780 entries of gameplay teasers.
 * That very nearly shipped, labelled as public-domain technical works, into a
 * bundle people would download expecting to learn how to make things.
 *
 * So the title and description now come from upstream rather than from whoever
 * is adding the entry, and both are printed for a human to look at.
 */
async function catalogMetadata(stem) {
  // The catalogue's free-text search is unreliable here -- "zimgit-water" and
  // "wikipedia_en_all" both return nothing at all -- but its exact "name="
  // filter works. Try the full stem first, then drop trailing segments, since
  // a flavour suffix like "_maxi" is sometimes part of the catalogue name and
  // sometimes not.
  const candidates = [];
  const parts = stem.split('_');
  for (let n = parts.length; n >= 2; n--) candidates.push(parts.slice(0, n).join('_'));

  for (const name of candidates) {
    let xml;
    try {
      // The catalogue serves an interstitial to unrecognised clients.
      const url = `https://library.kiwix.org/catalog/v2/entries?name=${encodeURIComponent(name)}&count=5`;
      xml = await (
        await get(url, { headers: { 'User-Agent': 'Mozilla/5.0 (Museion catalog-sync)' } })
      ).text();
    } catch {
      continue;
    }

    for (const entry of xml.split('<entry>').slice(1)) {
      const field = (tag) => entry.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`))?.[1]?.trim();
      const title = field('title');
      if (!title) continue;
      return {
        matchedName: name,
        title: unescapeXml(title),
        description: unescapeXml(field('summary') ?? ''),
        tags: unescapeXml(entry.match(/<tags>([^<]*)<\/tags>/)?.[1] ?? ''),
      };
    }
  }
  return null;
}

function unescapeXml(s) {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
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

  const upstream = await catalogMetadata(stem);
  if (upstream) {
    console.log(`  upstream title : ${upstream.title}`);
    if (upstream.description) console.log(`  upstream says  : ${upstream.description}`);
    if (upstream.tags) console.log(`  tags           : ${upstream.tags}`);
  } else {
    console.log('  !! not found in the Kiwix catalogue -- check by hand what this archive contains');
  }

  const doc = {
    id,
    // Upstream's title beats a guess from the filename; --title still wins,
    // for archives the catalogue does not know about.
    title:
      flags.title && flags.title !== true
        ? flags.title
        : upstream?.title || stem,
    ...(upstream?.description ? { description: upstream.description } : {}),
    // Whether upstream says the archive carries a usable full-text index.
    // Kiwix is honest about this -- the zimgit archives declare
    // _ftindex:no, which matches what measuring them showed -- so it is a
    // useful hint. Museion still probes behaviour at import rather than relying
    // on it, because the tag is absent for anything not in the catalogue.
    // See docs/retrieval.md.
    ...(upstream?.tags?.includes('_ftindex:')
      ? { claims_fulltext_index: upstream.tags.includes('_ftindex:yes') }
      : {}),
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
  // The recipe records --id explicitly so regeneration is idempotent. Without
  // it, any future change to how ids are derived from filenames silently
  // produces a second manifest under a new id rather than updating this one,
  // leaving the catalogue with duplicates and the bundles pointing at whichever
  // is now stale.
  // A --title given by the operator is part of the recipe too. Upstream
  // calls both the 12 GB summaries edition and the 49 GB full-text edition
  // of Wikipedia simply "Wikipedia"; regenerating from upstream alone turned
  // two distinguishable entries into two identical ones.
  const titleArg = flags.title && flags.title !== true ? ` --title ${JSON.stringify(flags.title)}` : '';
  const header =
    '# Generated by scripts/catalog-sync.js -- do not hand-edit size_bytes or sha256.\n' +
    `# Regenerate with: node scripts/catalog-sync.js add ${url} --category ${flags.category} --id ${id}${titleArg}\n`;
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
