// First-run set-up, as a wizard: one question per step, in plain words, and
// nothing downloads until the last step has shown the whole list and its
// size. The steps are the .wiz-step blocks in index.html, in STEPS below.
//
// This file is the steps; the last one, which lists what will be downloaded
// and then runs set-up, is welcome-run.js.

import { invoke } from './api.js';
import { pickInto } from './chooser.js';
import { $, el, gb } from './dom.js';
import { homeForm } from './home.js';
import { modelChooser } from './models.js';
import { leaveWelcome, onTab } from './tabs.js';
import { reviewAndRun } from './welcome-run.js';

const STEPS = ['start', 'folders', 'programs', 'model', 'library', 'home', 'review', 'done'];
const NAMES = {
  start: 'Start', folders: 'Folders', programs: 'Programs', model: 'Model', library: 'Library', home: 'Home area', review: 'Set up',
};

let plan = null;
let step = 'start';
let room = []; // free space for the library, models and helper folders
const home = homeForm($('#home-welcome'), { button: false });
const chooser = modelChooser($('#wiz-model-list'), { onChange: showSpare });

const folders = () => ({
  library: $('#wiz-library').value.trim(),
  models: $('#wiz-models').value.trim(),
  tools: $('#wiz-tools').value.trim(),
});

/** The steps this computer gets: Programs only where Museion has none to
 * download for it (plan.own_programs, from museion/own_programs.py). */
const order = () => STEPS.filter((s) => s !== 'programs' || plan?.own_programs?.length);

// ── moving between steps ────────────────────────────────────────────────────

function show(name) {
  step = name;
  for (const s of document.querySelectorAll('.wiz-step')) s.hidden = s.dataset.step !== name;
  const steps = $('#wiz-steps');
  steps.textContent = '';
  const ORDER = order();
  for (const id of ORDER.filter((s) => NAMES[s])) {
    const li = el('li', undefined, NAMES[id]);
    if (id === name) li.setAttribute('aria-current', 'step');
    if (ORDER.indexOf(id) < ORDER.indexOf(name)) li.className = 'done';
    steps.append(li);
  }
  steps.hidden = name === 'done';
  $('#wiz-nav').hidden = name === 'done';
  $('#welcome-skip-row').hidden = name === 'done';
  $('#wiz-back').hidden = name === 'start';
  $('#wiz-next').hidden = name === 'review';
  $('#wiz-go').hidden = name !== 'review';
  $('#wiz-next').textContent = name === 'start' ? 'Start' : 'Next';
  note('');
  // Each step starts at its top: Next is at the foot of a long step, and the
  // next one opened scrolled to its own foot.
  $('#panel-welcome .setup').scrollTop = 0;
}

function note(text, isError = false) {
  const n = $('#wiz-nav-note');
  n.textContent = text;
  n.className = `wiz-nav-note small-print ${isError ? 'err' : 'muted'}`;
}

/** Why the person cannot leave this step yet, or '' when they can. */
async function blocked() {
  if (step === 'start' && !plan) return 'Still looking at this computer; try again in a moment.';
  if (step === 'folders') {
    const f = folders();
    if (!f.library || !f.models || !f.tools) return 'Choose a folder first.';
    await showRoom();
    if (room.some((r) => r.free_bytes === null)) return 'Museion cannot use one of those folders. Check the drive is plugged in and the path is right.';
  }
  if (step === 'programs') {
    try {
      $('#wiz-own-note').textContent = await invoke('use_own_programs', { paths: ownPaths() });
    } catch (e) {
      return String(e);
    }
  }
  if (step === 'model' && !chooser.chosen()) return 'None of the models fits in that folder. Go back and choose a folder with more room.';
  return '';
}

async function enter(name) {
  if (name === 'model') await chooser.refresh(folders().models);
  if (name === 'review') await run.showReview();
  show(name);
}

$('#wiz-next').addEventListener('click', async () => {
  const why = await blocked();
  if (why) return note(why, true);
  await enter(order()[order().indexOf(step) + 1]);
});

$('#wiz-back').addEventListener('click', () => {
  if (run.running()) return;
  show(order()[order().indexOf(step) - 1]);
});

// ── step 1: this computer ───────────────────────────────────────────────────

async function loadPlan() {
  plan = null;
  show('start');
  run.reset();
  const machine = $('#wiz-machine ul');
  let cfg;
  // The folders first, from Settings: filled in after the plan, they would
  // overwrite whatever the person had typed while it loaded.
  try {
    cfg = await invoke('get_config');
    plan = await invoke('setup_plan');
    const choices = await invoke('model_choices');
    machine.textContent = '';
    for (const line of choices.machine) machine.append(el('li', undefined, line));
    const rec = choices.models.find((m) => m.recommended);
    if (rec) machine.append(el('li', undefined, `Museion's suggestion for it: ${rec.title}. You will see the other choices and why.`));
    if (plan.own_programs.length) {
      machine.append(el('li', undefined, 'Museion has no ready-made helper programs for this kind of computer, '
        + 'so one extra step asks where yours are.'));
    }
  } catch (e) {
    machine.textContent = '';
    machine.append(el('li', 'err', String(e)));
    return;
  }
  const tools = cfg.kiwix_exe?.replace(/[\\/][^\\/]*$/, '') ?? '';
  $('#wiz-portable').hidden = !plan.portable;
  fillFolders(cfg.library_dir ?? '', cfg.models_dir ?? '', tools);
  showOwn();
  show('start'); // again, now the plan says which steps this computer gets
  showBundles();
}

// ── step 2: folders ─────────────────────────────────────────────────────────

const join = (base, name) => (base ? `${base.replace(/[\\/]+$/, '')}${base.includes('\\') ? '\\' : '/'}${name}` : '');
const parent = (path) => path.replace(/[\\/]+$/, '').replace(/[\\/][^\\/]*$/, '');

/** One folder for everything, unless the three are already in different
 * places (a drive, or folders chosen by hand before). */
function fillFolders(library, models, tools) {
  const set = library || models || tools;
  if (!set) {
    $('#wiz-base').value = plan.suggested_base;
    spread();
  } else {
    $('#wiz-library').value = library;
    $('#wiz-models').value = models;
    $('#wiz-tools').value = tools;
    const together = library && models && tools && parent(library) === parent(models) && parent(models) === parent(tools);
    $('#wiz-base').value = together ? parent(library) : '';
    $('#wiz-separate').open = !together;
  }
  void showRoom();
}

/** The one folder typed: the three go inside it. */
function spread() {
  const base = $('#wiz-base').value.trim();
  $('#wiz-library').value = join(base, 'library');
  $('#wiz-models').value = join(base, 'models');
  $('#wiz-tools').value = join(base, 'bin');
}

let roomAsked = 0;
async function showRoom() {
  const f = folders();
  const asked = ++roomAsked;
  let got;
  try {
    got = await invoke('setup_room', { folders: [f.library, f.models, f.tools] });
  } catch {
    return;
  }
  if (asked !== roomAsked) return; // an older answer, for folders since retyped
  room = got;
  const drives = new Map();
  for (const r of room) if (r.free_bytes !== null) drives.set(r.volume, r.free_bytes);
  const p = $('#wiz-room');
  if (!f.library && !f.models && !f.tools) p.textContent = '';
  else if (room.some((r) => r.path && r.free_bytes === null)) p.textContent = 'One of these folders is on a drive Museion cannot read.';
  else p.textContent = `Free space: ${[...drives].map(([v, free]) => `${gb(free)} on ${v}`).join(', ')}.`;
}

let roomTimer = 0;
const roomSoon = () => {
  clearTimeout(roomTimer);
  roomTimer = setTimeout(showRoom, 400);
};
$('#wiz-base').addEventListener('input', () => {
  spread();
  roomSoon();
});
for (const id of ['#wiz-library', '#wiz-models', '#wiz-tools']) {
  $(id).addEventListener('input', () => {
    $('#wiz-base').value = ''; // typed apart: the one folder no longer says where they are
    roomSoon();
  });
}

async function pick(id, title) {
  await pickInto(id, true, title);
  if (id === '#wiz-base') spread();
  else $('#wiz-base').value = '';
  await showRoom();
}
$('#wiz-pick-base').addEventListener('click', () => pick('#wiz-base', "Museion's folder"));
$('#wiz-pick-library').addEventListener('click', () => pick('#wiz-library', 'Folder for the library'));
$('#wiz-pick-models').addEventListener('click', () => pick('#wiz-models', 'Folder for models'));
$('#wiz-pick-tools').addEventListener('click', () => pick('#wiz-tools', 'Folder for helper programs'));

// ── the person's own helper programs, where Museion has none to download ────

function showOwn() {
  const box = $('#wiz-own');
  box.textContent = '';
  for (const p of plan.own_programs) {
    const field = el('div', 'field');
    const label = el('label', undefined, p.label);
    const input = el('input');
    input.type = 'text';
    input.id = `wiz-own-${p.id}`;
    input.dataset.program = p.id;
    input.value = p.path;
    input.placeholder = p.kind === 'dir' ? 'Path to the folder' : `Path to ${p.id}`;
    label.htmlFor = input.id;
    const row = el('div', 'row');
    const pickButton = el('button', undefined, 'Choose…');
    pickButton.type = 'button';
    pickButton.addEventListener('click', () => pickInto(`#${CSS.escape(input.id)}`, p.kind === 'dir', p.label, p.kind === 'dir' ? undefined : 'program'));
    row.append(input, pickButton);
    field.append(label, el('span', 'what', p.path ? `${p.what} Found one on this computer.` : p.what), row);
    box.append(field);
  }
}

const ownPaths = () => Object.fromEntries([...document.querySelectorAll('#wiz-own input')].map((i) => [i.dataset.program, i.value.trim()]));

// ── step 3: the model ───────────────────────────────────────────────────────

/** The small model, when something bigger is chosen and it is not here yet. */
function spare() {
  const data = chooser.data();
  const chosen = chooser.chosen();
  const small = data?.models.find((m) => m.id === data.spare);
  if (!small || !chosen || small.id === chosen.id || small.on_disk || small.no_room) return null;
  return small;
}

function showSpare() {
  const small = spare();
  $('#wiz-spare-row').hidden = !small;
  if (small) $('#wiz-spare-label').textContent = `Also fetch the small model as a spare (${small.title}, ${gb(small.size_bytes)})`;
}

// ── step 4: the library ─────────────────────────────────────────────────────

function showBundles() {
  const box = $('#wiz-bundles');
  const before = $('#wiz-bundle').value || 'go-bag';
  box.textContent = '';
  $('#wiz-bundle').textContent = '';
  for (const b of plan.bundles) {
    // The select holds the choice, as it always has; the cards drive it.
    const o = el('option', undefined, b.title);
    o.value = b.id;
    $('#wiz-bundle').append(o);
    const card = el('label', 'model-choice');
    const head = el('div', 'model-head');
    const radio = el('input');
    radio.type = 'radio';
    radio.name = 'wiz-bundle-pick';
    radio.value = b.id;
    radio.checked = b.id === before;
    radio.addEventListener('change', () => {
      $('#wiz-bundle').value = b.id;
      for (const c of box.children) c.classList.toggle('chosen', c === card);
    });
    head.append(radio, el('span', 'model-title', b.title), el('span', 'model-size', `${gb(b.size_bytes)} to download`));
    card.append(head);
    if (b.id === 'go-bag') {
      const badges = el('div', 'badges');
      badges.append(el('span', 'badge good', 'A good first choice: done in minutes'));
      card.append(badges);
    }
    if (b.description) card.append(el('p', 'model-summary', b.description));
    card.classList.toggle('chosen', radio.checked);
    box.append(card);
  }
  $('#wiz-bundle').value = before;
}

// ── the last step: check, then run (welcome-run.js) ─────────────────────────

const run = reviewAndRun({
  plan: () => plan, room: () => room, chooser, home, folders, spare, showRoom, show, note,
});

$('#wiz-done').addEventListener('click', () => leaveWelcome('ask'));

$('#welcome-skip').addEventListener('click', (e) => {
  e.preventDefault();
  leaveWelcome('setup');
});

onTab('welcome', loadPlan);
onTab('welcome', home.refresh);
