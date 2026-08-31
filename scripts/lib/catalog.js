// Loading and validating the content catalog.
//
// The catalog is plain YAML on disk so that anyone can add a source without
// running Museion at all -- fork the repo, drop in a file, done. That means
// validation has to be explicit and the errors have to name the file, because
// the person hitting them may not be a programmer.

import { readFile, readdir } from 'node:fs/promises';
import { join, basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import YAML from 'yaml';

export const REPO_ROOT = fileURLToPath(new URL('../..', import.meta.url));
export const CATALOG_DIR = join(REPO_ROOT, 'catalog');

const REQUIRED_SOURCE_FIELDS = ['id', 'title', 'category', 'filename', 'size_bytes', 'sha256', 'urls'];

/** Categories a source may declare. Keep in sync with docs/adding-content.md. */
export const CATEGORIES = ['survival', 'medicine', 'making', 'reference'];

async function readYamlDir(dir) {
  let names;
  try {
    names = await readdir(dir);
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    throw err;
  }
  const out = [];
  for (const name of names.filter((n) => n.endsWith('.yaml') || n.endsWith('.yml'))) {
    const path = join(dir, name);
    const text = await readFile(path, 'utf8');
    let doc;
    try {
      doc = YAML.parse(text);
    } catch (err) {
      throw new Error(`${path}: not valid YAML -- ${err.message}`);
    }
    if (doc == null) throw new Error(`${path}: file is empty`);
    out.push({ path, doc });
  }
  return out;
}

function validateSource({ path, doc }) {
  const missing = REQUIRED_SOURCE_FIELDS.filter((f) => doc[f] == null);
  if (missing.length) {
    throw new Error(`${path}: missing required field(s): ${missing.join(', ')}`);
  }
  if (!Array.isArray(doc.urls) || doc.urls.length === 0) {
    throw new Error(`${path}: "urls" must be a non-empty list`);
  }
  if (!/^[0-9a-f]{64}$/.test(doc.sha256)) {
    throw new Error(`${path}: "sha256" must be 64 lowercase hex characters, got ${JSON.stringify(doc.sha256)}`);
  }
  if (!Number.isInteger(doc.size_bytes) || doc.size_bytes <= 0) {
    throw new Error(`${path}: "size_bytes" must be a positive integer`);
  }
  if (!CATEGORIES.includes(doc.category)) {
    throw new Error(`${path}: unknown category ${JSON.stringify(doc.category)}; expected one of ${CATEGORIES.join(', ')}`);
  }
  const expectedId = basename(path).replace(/\.ya?ml$/, '');
  if (doc.id !== expectedId) {
    throw new Error(`${path}: "id" is ${JSON.stringify(doc.id)} but the filename says ${JSON.stringify(expectedId)}; they must match`);
  }
  return doc;
}

/** Every source in the catalog, keyed by id. */
export async function loadSources() {
  const files = await readYamlDir(join(CATALOG_DIR, 'sources'));
  const byId = new Map();
  for (const file of files) {
    const src = validateSource(file);
    byId.set(src.id, { ...src, _path: file.path });
  }
  return byId;
}

/** Every bundle in the catalog, keyed by id. */
export async function loadBundles() {
  const files = await readYamlDir(join(CATALOG_DIR, 'bundles'));
  const byId = new Map();
  for (const { path, doc } of files) {
    if (!Array.isArray(doc.sources) || doc.sources.length === 0) {
      throw new Error(`${path}: "sources" must be a non-empty list of source ids`);
    }
    byId.set(doc.id, { ...doc, _path: path });
  }
  return byId;
}

/**
 * Resolve a bundle id (or a bare source id) to the list of sources to fetch.
 * Accepting either means "fetch go-bag" and "fetch zimgit-water" both work,
 * which is what people actually type.
 */
export async function resolveTarget(target) {
  const [sources, bundles] = await Promise.all([loadSources(), loadBundles()]);

  if (bundles.has(target)) {
    const bundle = bundles.get(target);
    const resolved = [];
    for (const id of bundle.sources) {
      const src = sources.get(id);
      if (!src) {
        throw new Error(`${bundle._path}: references unknown source ${JSON.stringify(id)}`);
      }
      resolved.push(src);
    }
    return { bundle, sources: resolved };
  }

  if (sources.has(target)) {
    return { bundle: null, sources: [sources.get(target)] };
  }

  const known = [...bundles.keys()].map((k) => `  ${k}  (bundle)`).concat([...sources.keys()].map((k) => `  ${k}`));
  throw new Error(`Unknown target ${JSON.stringify(target)}.\n\nAvailable:\n${known.join('\n')}`);
}

/** All candidate URLs for a source, primary first, then mirrors. */
export function urlsFor(source) {
  return [...source.urls, ...(source.mirrors ?? [])];
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}
