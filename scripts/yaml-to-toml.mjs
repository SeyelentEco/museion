#!/usr/bin/env node
// Writes the TOML twin of every data file the Python rewrite reads.
//
// The Rust app reads the YAML under catalog/, models/, tools/ and evals/, and
// crates/museion-core/tuning.yaml, until it is retired. The Python app reads
// TOML, because Python's standard library has a TOML parser (tomllib) and no
// YAML one. So the YAML stays the file to edit, and this script generates the
// TOML beside it, comments included: they hold the reasons for the choices
// (why this reranker conversion, why this offload split), and the reasons are
// what someone changing the file on their own most needs.
//
//   node scripts/yaml-to-toml.mjs           write every .toml
//   node scripts/yaml-to-toml.mjs --check   exit 1, naming the file, if any
//                                           committed .toml is out of date
//   node scripts/yaml-to-toml.mjs --json    print each file's data as JSON, for
//                                           tests/test_data.py to compare with
//                                           what tomllib reads
//
// Every comment in the YAML must land somewhere in the TOML; a comment in a
// place this script does not handle is an error, not a silent loss.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseDocument, isMap, isSeq, isScalar } from 'yaml';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DATA_DIRS = ['catalog/sources', 'catalog/bundles', 'models', 'tools'];
const SINGLE_FILES = {
  'evals/benchmark.yaml': 'evals/benchmark.toml',
  'crates/museion-core/tuning.yaml': 'museion/tuning.toml',
};

/** [yamlPath, tomlPath] pairs, relative to the repo root, '/'-separated. */
function pairs() {
  const out = [];
  for (const dir of DATA_DIRS) {
    for (const name of fs.readdirSync(path.join(ROOT, dir)).sort()) {
      if (name.endsWith('.yaml')) out.push([`${dir}/${name}`, `${dir}/${name.slice(0, -5)}.toml`]);
    }
  }
  for (const [y, t] of Object.entries(SINGLE_FILES)) out.push([y, t]);
  return out;
}

// ---------------------------------------------------------------- values

const BARE_KEY = /^[A-Za-z0-9_-]+$/;
const key = (k) => (BARE_KEY.test(k) ? k : basic(k));

/** A TOML basic string. JSON's escapes are TOML's, but TOML also bans DEL. */
function basic(s) {
  return JSON.stringify(s).replace(/\x7f/g, '\\u007F');
}

// Control characters other than tab cannot appear in a literal string.
const NEEDS_ESCAPE = /[\x00-\x08\x0a-\x1f\x7f]/;

function string(s) {
  if (s.includes('\n')) {
    // A literal multi-line string keeps a prompt exactly as written: no
    // escapes to read past, and a backslash is just a backslash. TOML drops
    // the newline right after the opening quotes.
    if (!s.includes("'''") && !NEEDS_ESCAPE.test(s.replace(/\n/g, ''))) return `'''\n${s}'''`;
    const body = s.replace(/\\/g, '\\\\').replace(/"""/g, '""\\"').replace(/[\x00-\x08\x0b-\x1f\x7f]/g,
      (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0').toUpperCase()}`);
    return `"""\n${body}"""`;
  }
  // Text full of quotes or backslashes reads better without escapes.
  if (/["\\]/.test(s) && !s.includes("'") && !NEEDS_ESCAPE.test(s)) return `'${s}'`;
  return basic(s);
}

function number(node, where) {
  const v = node.value;
  if (!Number.isFinite(v)) throw new Error(`${where}: ${node.source} has no TOML spelling`);
  const src = String(node.source ?? v);
  const looksFloat = /[.eE]/.test(src) || !Number.isInteger(v);
  if (!looksFloat) return String(v);
  const s = String(v);
  return /[.eE]/.test(s) ? s : `${s}.0`;
}

function scalar(node, where) {
  const v = node.value;
  if (v === null || v === undefined) throw new Error(`${where}: TOML has no null`);
  if (typeof v === 'boolean') return String(v);
  if (typeof v === 'number') return number(node, where);
  if (typeof v === 'string') return string(v);
  throw new Error(`${where}: cannot write a ${typeof v} (${node.source}) as TOML`);
}

// ---------------------------------------------------------------- comments

/** Every node carrying a comment, and the ones the output has used. */
class Comments {
  constructor() { this.used = new Set(); }
  lines(node, field, indent = '') {
    const text = node?.[field];
    if (!text) return [];
    this.used.add(`${field}:${id(node)}`);
    return text.split('\n').map((l) => `${indent}#${l}`.trimEnd());
  }
  trailing(node) {
    const text = node?.comment;
    if (!text) return '';
    if (text.includes('\n')) return null;
    this.used.add(`comment:${id(node)}`);
    return ` #${text}`;
  }
  assertAllUsed(doc, file) {
    const missed = [];
    const visit = (node, where) => {
      if (!node || typeof node !== 'object') return;
      for (const field of ['commentBefore', 'comment']) {
        if (node[field] && !this.used.has(`${field}:${id(node)}`)) missed.push(`${where}: ${JSON.stringify(node[field])}`);
      }
      if (isMap(node)) for (const p of node.items) { visit(p.key, `${where}.${p.key}`); visit(p.value, `${where}.${p.key}`); }
      if (isSeq(node)) node.items.forEach((it, i) => visit(it, `${where}[${i}]`));
    };
    visit(doc.contents, '');
    if (doc.comment && !this.used.has('doc.comment')) missed.push(`end of file: ${JSON.stringify(doc.comment)}`);
    if (missed.length) throw new Error(`${file}: comments with no place in the TOML:\n  ${missed.join('\n  ')}`);
  }
}

const ids = new WeakMap();
let nextId = 0;
function id(node) {
  if (!ids.has(node)) ids.set(node, ++nextId);
  return ids.get(node);
}

// ---------------------------------------------------------------- shapes

const hasComments = (n) => !!(n && (n.commentBefore || n.comment));

/** An array whose items are all tables: written as [[key]] sections. */
const isTableArray = (n) => isSeq(n) && !n.flow && n.items.length > 0 && n.items.every(isMap);

/** A small table of plain values reads best on one line, and keeps its place
    among the keys around it (a [section] would have to move to the end). */
function isInlineTable(n) {
  if (!isMap(n)) return false;
  if (n.flow) return true;
  return n.items.length <= 3 && n.items.every((p) =>
    isScalar(p.value) && !hasComments(p.key) && !hasComments(p.value) && !p.key.spaceBefore);
}

/** Written as its own [section] rather than as key = value. */
const isSection = (n) => (isMap(n) && !isInlineTable(n)) || isTableArray(n);

function inline(node, where, comments) {
  // The caller places the node's own comments; ones inside it have no place.
  if (isScalar(node)) return scalar(node, where);
  if (isMap(node)) {
    const parts = node.items.map((p) => {
      if (hasComments(p.key) || hasComments(p.value)) throw new Error(`${where}.${p.key}: comment inside an inline table`);
      return `${key(String(p.key.value))} = ${inline(p.value, `${where}.${p.key}`, comments)}`;
    });
    return parts.length ? `{ ${parts.join(', ')} }` : '{}';
  }
  if (isSeq(node)) {
    return `[${node.items.map((it, i) => {
      if (hasComments(it)) throw new Error(`${where}[${i}]: comment inside a one-line array`);
      return inline(it, `${where}[${i}]`, comments);
    }).join(', ')}]`;
  }
  throw new Error(`${where}: unsupported YAML node`);
}

/** key = value, with any block array spread one item per line as in the YAML. */
function keyValue(k, node, where, comments) {
  if (isSeq(node) && !node.flow && node.items.length > 0) {
    const out = [`${key(k)} = [`];
    node.items.forEach((it, i) => {
      out.push(...comments.lines(it, 'commentBefore', '  '));
      const tail = comments.trailing(it);
      if (tail === null) throw new Error(`${where}[${i}]: multi-line trailing comment`);
      out.push(`  ${inline(it, `${where}[${i}]`, comments)},${tail}`);
    });
    out.push(']', ...comments.lines(node, 'comment'));
    return out;
  }
  const tail = comments.trailing(node);
  if (tail === null) throw new Error(`${where}: multi-line trailing comment`);
  return [`${key(k)} = ${inline(node, where, comments)}${tail}`];
}

// ---------------------------------------------------------------- tables

/**
 * Writes one table: its plain keys first, then its sub-tables, since TOML
 * requires that order. `header` is the [name] or [[name]] line (null for the
 * root); `lead` is comment lines that belong just above it.
 */
function table(map, dotted, header, lead, comments, out) {
  const plain = map.items.filter((p) => !isSection(p.value));
  const sections = map.items.filter((p) => isSection(p.value));
  const trailing = comments.lines(map, 'comment');
  // [a] with nothing of its own but sub-tables is implied by [a.b], so its
  // comments move down to the first sub-table.
  const skipHeader = header && header.startsWith('[') && !header.startsWith('[[')
    && plain.length === 0 && sections.length > 0 && trailing.length === 0;
  if (header && !skipHeader) {
    if (out.length) out.push('');
    out.push(...lead, header);
    lead = [];
  }
  for (const p of plain) {
    const k = String(p.key.value);
    const where = `${dotted}.${k}`;
    if (p.key.spaceBefore && out.length && out[out.length - 1] !== '' && !out[out.length - 1].startsWith('[')) out.push('');
    out.push(...comments.lines(p.key, 'commentBefore'), ...comments.lines(p.value, 'commentBefore'));
    if (hasComments(p.key) && p.key.comment) throw new Error(`${where}: comment after a key`);
    out.push(...keyValue(k, p.value, where, comments));
  }
  out.push(...trailing);
  for (const p of sections) {
    const k = String(p.key.value);
    const name = dotted ? `${dotted}.${key(k)}` : key(k);
    const before = [...lead, ...comments.lines(p.key, 'commentBefore'), ...comments.lines(p.value, 'commentBefore')];
    lead = [];
    if (isMap(p.value)) {
      table(p.value, name, `[${name}]`, before, comments, out);
    } else {
      if (p.value.comment) throw new Error(`${name}: trailing comment on an array of tables`);
      p.value.items.forEach((item, i) => {
        const itemLead = i === 0 ? [...before, ...comments.lines(item, 'commentBefore')] : comments.lines(item, 'commentBefore');
        table(item, name, `[[${name}]]`, itemLead, comments, out);
      });
    }
  }
}

function toToml(yamlRel, text) {
  const doc = parseDocument(text, { prettyErrors: true });
  if (doc.errors.length) throw new Error(`${yamlRel}: ${doc.errors[0].message}`);
  if (!isMap(doc.contents)) throw new Error(`${yamlRel}: the top level must be a mapping`);
  const comments = new Comments();
  const out = [
    `# Generated from ${yamlRel} by scripts/yaml-to-toml.mjs. Edit the YAML, not this file, until the Rust app is retired.`,
    '',
  ];
  const head = [...comments.lines(doc, 'commentBefore'), ...comments.lines(doc.contents, 'commentBefore')];
  if (head.length) out.push(...head, '');
  const body = [];
  table(doc.contents, '', null, [], comments, body);
  out.push(...body);
  if (doc.comment) {
    comments.used.add('doc.comment');
    out.push('', ...doc.comment.split('\n').map((l) => `#${l}`.trimEnd()));
  }
  comments.assertAllUsed(doc, yamlRel);
  return out.join('\n').replace(/\n{3,}/g, '\n\n') + '\n';
}

// ---------------------------------------------------------------- main

function main(argv) {
  const mode = argv[0] ?? '--write';
  if (!['--write', '--check', '--json'].includes(mode)) {
    console.error('usage: node scripts/yaml-to-toml.mjs [--check | --json]');
    return 2;
  }
  if (mode === '--json') {
    const all = {};
    for (const [y, t] of pairs()) {
      all[y] = { toml: t, data: parseDocument(fs.readFileSync(path.join(ROOT, y), 'utf8')).toJS() };
    }
    process.stdout.write(JSON.stringify(all));
    return 0;
  }
  const stale = [];
  const wanted = new Set();
  for (const [y, t] of pairs()) {
    wanted.add(t);
    const toml = toToml(y, fs.readFileSync(path.join(ROOT, y), 'utf8'));
    const dest = path.join(ROOT, t);
    const current = fs.existsSync(dest) ? fs.readFileSync(dest, 'utf8') : null;
    if (current === toml) continue;
    if (mode === '--check') stale.push(`${t} ${current === null ? 'is missing' : `differs from what ${y} generates`}`);
    else { fs.writeFileSync(dest, toml); console.log(`wrote ${t}`); }
  }
  // A .toml with no YAML behind it would be read by Python and never by Rust.
  for (const dir of DATA_DIRS) {
    for (const name of fs.readdirSync(path.join(ROOT, dir))) {
      if (name.endsWith('.toml') && !wanted.has(`${dir}/${name}`)) stale.push(`${dir}/${name} has no YAML to generate it from`);
    }
  }
  if (stale.length) {
    console.error(`${stale.join('\n')}\nRun: node scripts/yaml-to-toml.mjs`);
    return 1;
  }
  if (mode === '--check') console.log(`all ${wanted.size} TOML files match their YAML`);
  return 0;
}

try {
  process.exitCode = main(process.argv.slice(2));
} catch (e) {
  console.error(e.message);
  process.exitCode = 1;
}
