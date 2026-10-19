// The Choose… window. A browser page cannot open the system's file dialog
// and learn a path from it, so Museion lists this computer's folders itself
// (the browse command in commands.py) and the person picks from the list.

import { invoke } from './api.js';
import { $, el, gb } from './dom.js';

let current = null; // { want, resolve, path }

/**
 * Ask for a folder or file. `want` is 'dir', 'gguf' or 'program'. Resolves
 * with the chosen path, or null if the person cancels.
 */
export function chooseFile({ title, want = 'dir', start = '' }) {
  if (current) current.resolve(null);
  return new Promise((resolve) => {
    current = { want, resolve, path: '' };
    $('#chooser-title').textContent = title;
    $('#chooser-pick').hidden = want !== 'dir';
    $('#chooser').hidden = false;
    void show(start);
  });
}

/** Fill the input `id` with a chosen path, starting from what it holds. */
export async function pickInto(id, directory, title, want) {
  const input = $(id);
  const chosen = await chooseFile({ title, want: directory ? 'dir' : want ?? 'any', start: input.value });
  if (chosen) input.value = chosen;
}

async function show(path) {
  const list = $('#chooser-list');
  let r;
  try {
    r = await invoke('browse', { path, want: current?.want ?? 'dir' });
  } catch (e) {
    list.textContent = String(e);
    return;
  }
  if (!current) return;
  current.path = r.path;
  current.parent = r.parent;
  $('#chooser-path').value = r.path;
  $('#chooser-up').disabled = !r.parent;

  const roots = $('#chooser-roots');
  roots.textContent = '';
  for (const root of r.roots) {
    const b = el('button', undefined, root.name);
    b.type = 'button';
    b.addEventListener('click', () => void show(root.path));
    roots.append(b);
  }

  list.textContent = '';
  if (r.error) list.append(el('div', 'entry', r.error));
  for (const e of r.entries) {
    const row = el('div', 'entry');
    row.append(el('span', 'kind', e.dir ? '▸' : '·'));
    row.append(el('span', undefined, e.name));
    if (!e.dir) row.append(el('span', 'size', gb(e.size_bytes)));
    const full = joinPath(r.path, e.name);
    row.addEventListener('click', () => (e.dir ? void show(full) : finish(full)));
    list.append(row);
  }
  if (!r.entries.length && !r.error) {
    list.append(el('div', 'entry', current.want === 'dir' ? 'No folders here.' : 'Nothing to choose here.'));
  }
}

function joinPath(dir, name) {
  const sep = dir.includes('\\') && !dir.includes('/') ? '\\' : '/';
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}

function finish(path) {
  $('#chooser').hidden = true;
  const c = current;
  current = null;
  c?.resolve(path);
}

$('#chooser-up').addEventListener('click', () => current?.parent && void show(current.parent));
$('#chooser-go').addEventListener('click', () => void show($('#chooser-path').value));
$('#chooser-path').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') void show($('#chooser-path').value);
});
$('#chooser-pick').addEventListener('click', () => finish(current?.path ?? null));
$('#chooser-cancel').addEventListener('click', () => finish(null));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && current) finish(null);
});
