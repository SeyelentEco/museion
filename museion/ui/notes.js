// The Notes tab: the person's own notes and records (museion/notes.py). A
// list of the files, an editor, and for a record a table of its rows. Its
// first page is "Coming up": what is due now and soon, from the almanac,
// maintenance and medicines (museion/almanac.py).

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
  const coming = el('li', current ? 'coming' : 'coming active');
  coming.append(el('div', 't', 'Coming up'));
  coming.append(el('div', 'meta', 'what to do now and soon'));
  coming.addEventListener('click', () => void showComing());
  list.append(coming);
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

const KIND = { garden: 'Garden', preserving: 'Preserving', animals: 'Animals', maintenance: 'Maintenance',
  medicines: 'Medicines', stores: 'Stores' };

function whenText(item) {
  if (item.status === 'overdue') return `Overdue ${-item.days} d`;
  if (item.status === 'now') return 'Now';
  return item.days === 1 ? 'Tomorrow' : `In ${item.days} days`;
}

/** The first page: what is due now and in the next weeks. */
export async function showComing() {
  if (dirty() && !confirm(`${current.name} has changes that are not saved. Leave it anyway?`)) return;
  current = null;
  $('#note-edit').hidden = true;
  $('#note-empty').hidden = false;
  document.querySelectorAll('#note-items li').forEach((li) => li.classList.toggle('active', li.classList.contains('coming')));
  let data;
  try {
    data = await invoke('coming_up');
  } catch (e) {
    $('#coming-items').textContent = '';
    $('#coming-about').textContent = String(e);
    return;
  }
  drawComing(data);
}

function drawComing(data) {
  const list = $('#coming-items');
  list.textContent = '';
  for (const item of data.items) {
    const li = el('li', item.status);
    li.append(el('span', 'when', whenText(item)));
    const what = el('div', 'what');
    what.append(el('div', 't', item.task));
    const until = item.status === 'now' && item.end > data.today ? ` · until ${item.end}` : '';
    const meta = el('div', 'meta', `${KIND[item.kind] ?? item.kind}${item.when ? ` · ${item.when}` : ''}${until} · `);
    const open = el('button', undefined, item.source);
    open.type = 'button';
    open.title = `Open ${item.source}`;
    open.addEventListener('click', () => void openNote(item.source));
    meta.append(open);
    if (item.notes) meta.title = item.notes;
    what.append(meta);
    li.append(what);
    list.append(li);
  }
  if (!data.items.length) {
    list.append(el('li', 'none', `Nothing is due in the next ${data.weeks} weeks.`));
  }
  $('#coming-start').hidden = !data.can_start;
  const waiting = Object.entries(data.needs_frost);
  $('#frost-form').hidden = data.can_start;
  $('#frost-last').value = data.frosts.last ?? '';
  $('#frost-first').value = data.frosts.first ?? '';
  const zone = data.zone ? ` Hardiness zone ${data.zone}.` : '';
  $('#frost-about').textContent = waiting.length
    ? `${waiting.map(([w, n]) => `${n} task${n === 1 ? ' is' : 's are'} timed from your ${w} frost`).join(', and ')}: give your average frost dates, as month-day. Local growers, old seed packets or the library's climate maps say.`
    : `Your average frost dates, as month-day. The almanac times its tasks from them.${zone}`;
  $('#frost-form').title = data.frost_note ?? '';
  $('#climate-look').hidden = !data.climate;
  $('#climate-setup').hidden = Boolean(data.climate);
  if (data.home && !$('#climate-place').value) $('#climate-place').value = data.home;
  $('#climate-place').title = data.climate ? `Towns in the climate table's region: ${data.climate}` : '';
  const problems = $('#coming-problems');
  problems.textContent = '';
  for (const p of data.problems) problems.append(el('li', undefined, p));
}

function show(note) {
  current = { name: note.name, text: note.text };
  $('#note-empty').hidden = true;
  $('#note-edit').hidden = false;
  $('#note-name').textContent = note.name;
  $('#note-name').title = note.path;
  $('#note-text').value = note.text;
  $('#note-text').classList.toggle('csv', note.name.toLowerCase().endsWith('.csv'));
  const kind = kinds.find((k) => k.file === note.name);
  $('#note-about').textContent = kind ? `${kind.about} One row per line; the first line names the columns.` : '';
  setState('');
  drawTable();
  document.querySelectorAll('#note-items li').forEach((li) => li.classList.toggle('active', li.dataset.name === note.name));
}

$('#coming-start-go').addEventListener('click', async () => {
  try {
    await invoke('new_note', { name: '', record: 'almanac.csv' });
    await refresh();
    await showComing();
    $('#frost-last').focus();
  } catch (e) {
    alert(String(e));
  }
});

$('#frost-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    drawComing(await invoke('set_frost_dates', { last: $('#frost-last').value, first: $('#frost-first').value }));
    $('#frost-state').textContent = 'Saved in almanac.csv.';
    await refresh();
  } catch (err) {
    $('#frost-state').textContent = String(err);
  }
});

let looked = null;  // the last look-up, until it is used

async function lookUp(pick = 0) {
  const result = $('#climate-result');
  result.hidden = false;
  $('#climate-use').hidden = true;
  result.textContent = 'Looking up…';
  try {
    looked = await invoke('climate_lookup', { place: $('#climate-place').value, pick });
  } catch (err) {
    looked = null;
    // A new search that failed has nothing to pick from; a pick that failed
    // (a town far outside the table) leaves the others to pick.
    if (!pick) $('#climate-pick').hidden = true;
    result.textContent = String(err);
    return;
  }
  const choose = $('#climate-pick');
  choose.textContent = '';
  looked.places.forEach((p, i) => choose.append(new Option(p.label, String(i), false, i === looked.picked)));
  choose.hidden = looked.places.length < 2;
  result.textContent = looked.text;
  const e = looked.estimate;
  $('#climate-use').hidden = !(e.last_frost || e.first_frost || e.zone);
}

$('#climate-go').addEventListener('click', () => void lookUp());
$('#climate-place').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    void lookUp();
  }
});
$('#climate-pick').addEventListener('change', () => void lookUp(Number($('#climate-pick').value)));
$('#climate-use').addEventListener('click', async () => {
  if (!looked) return;
  const e = looked.estimate;
  try {
    drawComing(await invoke('set_frost_dates', { last: e.last_frost, first: e.first_frost, zone: e.zone, note: looked.text }));
    $('#frost-state').textContent = 'Saved in almanac.csv, with where the dates came from.';
    $('#climate-use').hidden = true;
    await refresh();
  } catch (err) {
    $('#frost-state').textContent = String(err);
  }
});

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

onTab('notes', async () => {
  await refresh();
  if (!current) await showComing();
});
