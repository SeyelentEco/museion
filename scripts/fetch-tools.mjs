#!/usr/bin/env node
// Download and unpack the helper programs Museion drives: kiwix-serve for
// content, llama-server for inference, pdfium for reading PDFs.
//
// These are not vendored into the repo. They are large, platform-specific, and
// belong to other projects; committing them would bloat the repo and mean
// shipping someone else's binaries under our name. They are fetched from their
// own publishers and verified against pinned checksums.
//
//   node scripts/fetch-tools.mjs --dest ./bin
//   node scripts/fetch-tools.mjs --dest ./bin --accelerator vulkan
//   node scripts/fetch-tools.mjs --list

import { readFile, readdir, mkdir, rm, rename, readdir as readdirp } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, basename } from 'node:path';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import YAML from 'yaml';
import { REPO_ROOT, formatBytes } from './lib/catalog.js';
import { downloadVerified, isVerified } from './lib/download.js';
import { parseArgs, renderProgress, clearProgress, fail } from './lib/cli.js';

const TOOLS_DIR = join(REPO_ROOT, 'tools');

async function loadTools() {
  const names = (await readdir(TOOLS_DIR)).filter((n) => n.endsWith('.yaml'));
  const out = [];
  for (const n of names) {
    out.push(YAML.parse(await readFile(join(TOOLS_DIR, n), 'utf8')));
  }
  return out;
}

/** What this machine looks like, in the terms the manifests use. */
function platform() {
  const p = process.platform === 'win32' ? 'windows' : process.platform;
  const arch = process.arch === 'x64' ? 'x64' : process.arch === 'arm64' ? 'arm64' : process.arch;
  return { platform: p, arch };
}

/**
 * Which accelerator to build for. Detection is deliberately simple -- the only
 * question is which llama.cpp package to fetch, and the CPU build always works,
 * so guessing wrong costs speed rather than correctness.
 */
function detectAccelerator() {
  if (process.platform === 'darwin') return process.arch === 'arm64' ? 'metal' : 'cpu';
  try {
    execFileSync('nvidia-smi', ['--query-gpu=name', '--format=csv,noheader'], { stdio: 'pipe' });
    return 'cuda';
  } catch {
    return process.platform === 'win32' ? 'vulkan' : 'cpu';
  }
}

function pickVariant(tool, host, accelerator) {
  const entries = Object.entries(tool.variants ?? {});
  const matches = entries.filter(([, v]) => {
    if (v.internal) return false;
    if (v.platform && v.platform !== host.platform) return false;
    if (v.arch && v.arch !== host.arch) return false;
    return true;
  });
  if (!matches.length) return null;

  // Prefer the requested accelerator, then anything unlabelled, then CPU.
  const byAccel =
    matches.find(([, v]) => v.accelerator === accelerator) ??
    matches.find(([, v]) => !v.accelerator) ??
    matches.find(([, v]) => v.accelerator === 'cpu');
  return byAccel ? { key: byAccel[0], ...byAccel[1] } : null;
}

/**
 * Unpack an archive using whatever the system already has.
 *
 * bsdtar handles both .zip and .tar.gz and ships with Windows 10+ and macOS,
 * so it is tried first. GNU tar (common on Linux) cannot read zip, hence the
 * unzip fallback.
 */
function extract(archive, into) {
  const isZip = archive.toLowerCase().endsWith('.zip');
  const candidates = [
    // bsdtar first, for both formats. It auto-detects compression, and unlike
    // GNU tar it does not read a Windows drive letter as a remote host --
    // "tar -xzf C:\..." fails with "Cannot connect to C: resolve failed".
    ['C:\\Windows\\System32\\tar.exe', ['-xf', archive, '-C', into]],
    ['tar', ['-xf', '--force-local', archive, '-C', into]],
    ['tar', [isZip ? '-xf' : '-xzf', '--force-local', archive, '-C', into]],
    ['tar', [isZip ? '-xf' : '-xzf', archive, '-C', into]],
    ...(isZip ? [['unzip', ['-oq', archive, '-d', into]]] : []),
  ];

  let last;
  for (const [cmd, args] of candidates) {
    try {
      execFileSync(cmd, args, { stdio: 'pipe' });
      return;
    } catch (e) {
      last = e;
    }
  }
  throw new Error(
    `could not unpack ${basename(archive)}.\n  Tried: ${candidates.map((c) => c[0]).join(', ')}\n  ${last?.message ?? ''}`,
  );
}

/**
 * Flatten whatever directory layout an archive happens to use.
 *
 * The publishers do not agree: kiwix-tools puts its binaries at the top level,
 * llama.cpp nests them under build/bin on some platforms, and pdfium ships a
 * bin/ and a lib/. Museion just wants everything executable in one directory.
 */
async function flatten(dir) {
  const wanted = /\.(exe|dll|dylib|so(\.\d+)*)$/i;
  const moved = [];
  async function walk(d, depth = 0) {
    if (depth > 4) return;
    for (const entry of await readdirp(d, { withFileTypes: true })) {
      const p = join(d, entry.name);
      if (entry.isDirectory()) {
        await walk(p, depth + 1);
      } else if (d !== dir && (wanted.test(entry.name) || !entry.name.includes('.'))) {
        const target = join(dir, entry.name);
        if (!existsSync(target)) {
          await rename(p, target);
          moved.push(entry.name);
        }
      }
    }
  }
  await walk(dir);
  return moved;
}

async function main() {
  const { flags } = parseArgs(process.argv.slice(2));
  const tools = await loadTools();
  const host = platform();
  const accelerator = flags.accelerator && flags.accelerator !== true ? flags.accelerator : detectAccelerator();

  if (flags.list) {
    console.log('');
    for (const t of tools) {
      console.log(`  ${t.name} ${t.version}  — provides ${t.provides.join(', ')}`);
      for (const [k, v] of Object.entries(t.variants)) {
        console.log(`      ${k.padEnd(16)} ${formatBytes(v.size_bytes).padStart(9)}`);
      }
    }
    console.log('');
    return;
  }

  const dest = flags.dest && flags.dest !== true ? flags.dest : null;
  if (!dest) {
    throw new Error('pass --dest <dir> (where the helper programs should live)');
  }
  await mkdir(dest, { recursive: true });

  console.log(`\nPlatform: ${host.platform}/${host.arch}, accelerator: ${accelerator}`);
  console.log(`Into:     ${dest}\n`);

  const staging = join(tmpdir(), `museion-tools-${process.pid}`);
  await mkdir(staging, { recursive: true });

  try {
    for (const tool of tools) {
      const variant = pickVariant(tool, host, accelerator);
      if (!variant) {
        console.log(`  --       ${tool.name}: no build for ${host.platform}/${host.arch}`);
        continue;
      }

      // Some variants need a companion archive: the Windows CUDA build of
      // llama.cpp ships without the CUDA runtime DLLs, which live separately.
      const parts = [variant, ...(variant.also ?? []).map((k) => ({ key: k, ...tool.variants[k] }))];

      for (const part of parts) {
        const archive = join(staging, part.file);
        if (!(await isVerified(archive, part))) {
          try {
            await downloadVerified({
              urls: [part.url],
              dest: archive,
              size_bytes: part.size_bytes,
              sha256: part.sha256,
              onProgress: (p) => renderProgress(part.file, p),
            });
            clearProgress();
          } catch (e) {
            clearProgress();
            throw new Error(`fetching ${part.file}: ${e.message}`);
          }
        }
        extract(archive, dest);
        console.log(`  ok       ${tool.name} (${part.key})  ${formatBytes(part.size_bytes)}`);
      }
    }

    const moved = await flatten(dest);
    if (moved.length) console.log(`\n  flattened ${moved.length} file(s) into ${dest}`);

    console.log('\nInstalled:');
    for (const f of (await readdir(dest)).filter((n) => /\.(exe|dylib|so)$/.test(n) || !n.includes('.'))) {
      console.log(`  ${f}`);
    }
    console.log('');
  } finally {
    await rm(staging, { recursive: true, force: true });
  }
}

main().catch(fail);
