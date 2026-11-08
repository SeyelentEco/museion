// The Notes tab: the person's own notes and records (museion/notes.py). A
// list of the files, an editor, and for a record a table of its rows.

import { invoke } from './api.js';
import { $, el } from './dom.js';
import { onTab, showTab } from './tabs.js';

// The note on screen: { name, text } as last loaded or saved.
let current = null;
let kinds = [];

function dirty() {
  return current !== null && $('#note-text').value !== current.text;
}

function setState(text) {
  $('#note-state').textContent = text;
}

async function refresh() {
  let data;
  try {
    data = await invoke('list_notes');
  } catch (e) {
    const list = $('#note-items');
    list.textContent = '';
    list.append(el('li', 'empty-note', String(e)));
    return;
  }
  kinds = data.kinds;
  const list = $('#note-items');
  list.textContent = '';
  if (!data.notes.length) list.append(el('li', 'empty-note', 'No notes yet.'));
  for (const n of data.notes) {
    const li = el('li', current?.name === n.name ? 'active' : undefined);
    li.dataset.name = n.name;
    li.append(el('div', 't', n.name));
    li.append(el('div', 'meta', `changed ${new Date(n.modified * 1000).toLocaleDateString()}`));
    li.addEventListener('click', () => void openNote(n.name));
    list.append(li);
  }
  const pick = $('#note-record');
  pick.textContent = '';
  pick.append(el('option', undefined, 'New record…'));
  pick.firstChild.value = '';
  for (const k of data.kinds) {
    const o = el('option', undefined, k.have ? `${k.title} (open)` : k.title);
    o.value = k.file;
    o.title = k.about;
    pick.append(o);
  }
}

/** Show a note in the editor; asks first if the one on screen is unsaved. */
export async function openNote(name) {
  if (dirty() && current.name !== name && !confirm(`${current.name} has changes that are not saved. Leave it anyway?`)) return;
  let note;
  try {
    note = await invoke('read_note', { name });
  } catch (e) {
    alert(String(e));
    return;
  }
  show(note);
}

function show(note) {
  current = { name: note.name, text: note.text };
  $('#note-empty').hidden = true;
  $('#note-edit').hidden = false;
  $('#note-name').textContent = note.name;
  $('#note-name').title = note.path;
  $('#note-text').value = note.text;
  const kind = kinds.find((k) => k.file === note.name);
  $('#note-about').textContent = kind ? `${kind.about} One row per line; the first line names the columns.` : '';
  setState('');
  drawTable();
  document.querySelectorAll('#note-items li').forEach((li) => li.classList.toggle('active', li.dataset.name === note.name));
}

/** A CSV line split into cells, with "quoted, cells" kept whole. */
function cells(line) {
  const out = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') {
        quoted = false;
      } else {
        cell += c;
      }
    } else if (c === '"') {
      quoted = true;
    } else if (c === ',') {
      out.push(cell);
      cell = '';
    } else {
      cell += c;
    }
  }
  out.push(cell);
  return out;
}

function drawTable() {
  const box = $('#note-table');
  box.textContent = '';
  if (!current?.name.toLowerCase().endsWith('.csv')) return;
  const lines = $('#note-text').value.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return;
  const table = el('table');
  const head = el('tr');
  for (const h of cells(lines[0])) head.append(el('th', undefined, h));
  table.append(head);
  for (const line of lines.slice(1)) {
    const tr = el('tr');
    for (const c of cells(line)) tr.append(el('td', undefined, c));
    table.append(tr);
  }
  box.append(table);
}

async function save() {
  if (!current) return;
  try {
    const note = await invoke('save_note', { name: current.name, text: $('#note-text').value });
    current = { name: note.name, text: note.text };
    $('#note-text').value = note.text;
    setState('Saved');
    drawTable();
    await refresh();
  } catch (e) {
    setState(String(e));
  }
}

$('#note-text').addEventListener('input', () => {
  setState(dirty() ? 'Not saved' : '');
  drawTable();
});
$('#note-text').addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault();
    void save();
  }
});
$('#note-save').addEventListener('click', () => void save());

$('#note-new').addEventListener('click', async () => {
  if (dirty() && !confirm(`${current.name} has changes that are not saved. Leave it anyway?`)) return;
  const name = prompt('Name of the new note (for example pump-repair or seeds-2027):');
  if (!name?.trim()) return;
  try {
    show(await invoke('new_note', { name }));
    await refresh();
    $('#note-text').focus();
  } catch (e) {
    alert(String(e));
  }
});

$('#note-record').addEventListener('change', async (e) => {
  const file = e.target.value;
  e.target.value = '';
  if (!file) return;
  if (dirty() && !confirm(`${current.name} has changes that are not saved. Leave it anyway?`)) return;
  try {
    show(await invoke('new_note', { name: '', record: file }));
    await refresh();
  } catch (err) {
    alert(String(err));
  }
});

$('#note-folder').addEventListener('click', () => {
  invoke('open_notes_folder').catch((e) => alert(String(e)));
});

/** Open a cited note: the Notes tab, at that note. */
export async function openCitedNote(name) {
  showTab('notes');
  await openNote(name);
}

onTab('notes', refresh);
