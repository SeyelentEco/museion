// First-run set-up, as a wizard: one question per step, in plain words, and
// nothing downloads until the last step has shown the whole list and its
// size. The steps are the .wiz-step blocks in index.html, in ORDER below.
//
// Set-up itself is the same run it always was -- programs, reranker, chat
// model, content, fit, services, index, home area -- each part resumable, so
// a failure partway costs a retry, not a restart.

import { invoke, listen } from './api.js';
import { pickInto } from './chooser.js';
import { $, el, gb, statusMsg } from './dom.js';
import { refreshServiceDots } from './dots.js';
import { homeForm } from './home.js';
import { modelChooser } from './models.js';
import { refreshSetup } from './settings.js';
import { leaveWelcome, onTab } from './tabs.js';

const ORDER = ['start', 'folders', 'model', 'library', 'home', 'review', 'done'];
const NAMES = { start: 'Start', folders: 'Folders', model: 'Model', library: 'Library', home: 'Home area', review: 'Set up' };
// What your home area downloads, for the list on the last step (home.js says the same).
const HOME_BYTES = 150 * 1048576;

let plan = null;
let step = 'start';
let running = false;
let room = []; // free space for the library, models and helper folders
const home = homeForm($('#home-welcome'), { button: false });
const chooser = modelChooser($('#wiz-model-list'), { onChange: showSpare });

const folders = () => ({
  library: $('#wiz-library').value.trim(),
  models: $('#wiz-models').value.trim(),
  tools: $('#wiz-tools').value.trim(),
});

// ── moving between steps ────────────────────────────────────────────────────

function show(name) {
  step = name;
  for (const s of document.querySelectorAll('.wiz-step')) s.hidden = s.dataset.step !== name;
  const steps = $('#wiz-steps');
  steps.textContent = '';
  for (const id of ORDER.filter((s) => NAMES[s])) {
    const li = el('li', undefined, NAMES[id]);
    if (id === name) li.setAttribute('aria-current', 'step');
    if (ORDER.indexOf(id) < ORDER.indexOf(name)) li.className = 'done';
    steps.append(li);
  }
  steps.hidden = name === 'done';
  $('#wiz-nav').hidden = name === 'done';
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
  if (step === 'model' && !chooser.chosen()) return 'None of the models fits in that folder. Go back and choose a folder with more room.';
  return '';
}

async function enter(name) {
  if (name === 'model') await chooser.refresh(folders().models);
  if (name === 'review') await showReview();
  show(name);
}

$('#wiz-next').addEventListener('click', async () => {
  const why = await blocked();
  if (why) return note(why, true);
  await enter(ORDER[ORDER.indexOf(step) + 1]);
});

$('#wiz-back').addEventListener('click', () => {
  if (running) return;
  show(ORDER[ORDER.indexOf(step) - 1]);
});

// ── step 1: this computer ───────────────────────────────────────────────────

async function loadPlan() {
  plan = null;
  show('start');
  resetRun();
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
    if (!plan.tools.length) {
      machine.append(el('li', 'err', 'The catalogue has no helper programs for this system (Linux has none yet). '
        + 'Set-up will fetch the models and library; point Settings at your own llama-server and kiwix-serve.'));
    }
  } catch (e) {
    machine.textContent = '';
    machine.append(el('li', 'err', String(e)));
    return;
  }
  const tools = cfg.kiwix_exe?.replace(/[\\/][^\\/]*$/, '') ?? '';
  $('#wiz-portable').hidden = !plan.portable;
  fillFolders(cfg.library_dir ?? '', cfg.models_dir ?? '', tools);
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

// ── step 6: check, then run ─────────────────────────────────────────────────

/** Everything set-up will fetch: { title, bytes, where } with `where` an
 * index into `room` (library, models, helper programs). */
function items() {
  const out = [];
  // A release or a prepared drive already carries its helper programs.
  if (!plan.portable) for (const t of plan.tools) out.push({ title: t.title, what: 'Helper program', bytes: t.size_bytes, where: 2 });
  if (plan.reranker) {
    const have = chooser.data()?.reranker_on_disk;
    out.push({ title: plan.reranker.title, what: 'Puts the best passages first', bytes: have ? 0 : plan.reranker.size_bytes, where: 1, have });
  }
  const model = chooser.chosen();
  if (model) out.push({ title: model.title, what: 'The model that writes the answers', bytes: model.on_disk ? 0 : model.size_bytes, where: 1, have: model.on_disk });
  if (spare() && $('#wiz-spare').checked) out.push({ title: spare().title, what: 'Spare model for any computer', bytes: spare().size_bytes, where: 1 });
  const bundle = plan.bundles.find((b) => b.id === $('#wiz-bundle').value);
  if (bundle) out.push({ title: bundle.title, what: `Library: ${bundle.description}`, bytes: bundle.size_bytes, where: 0 });
  if (home.wanted()) out.push({ title: 'Your home area', what: 'Weather records and towns near you', bytes: HOME_BYTES, where: 0, about: true });
  return out;
}

/** Fills the list and says whether it all fits. */
async function showReview() {
  await showRoom();
  const table = $('#wiz-review');
  table.textContent = '';
  const list = items();
  for (const it of list) {
    const tr = el('tr');
    const name = el('td');
    name.append(el('div', 't', it.title), el('div', 'meta', it.what));
    tr.append(name, el('td', 'size', it.have ? 'already here' : `${it.about ? 'about ' : ''}${gb(it.bytes)}`));
    table.append(tr);
  }
  const total = list.reduce((n, it) => n + it.bytes, 0);
  const tr = el('tr', 'total');
  tr.append(el('td', undefined, 'Total to download'), el('td', 'size', gb(total)));
  table.append(tr);

  // Room, drive by drive: the three folders may share one or not.
  const need = new Map();
  for (const it of list) {
    const r = room[it.where];
    if (r) need.set(r.volume, (need.get(r.volume) ?? 0) + it.bytes);
  }
  const short = [];
  const fine = [];
  for (const [volume, bytes] of need) {
    const free = room.find((r) => r.volume === volume)?.free_bytes ?? 0;
    // The index and unpacking need a little beyond the downloads themselves.
    if (bytes * 1.05 > free) short.push(`${volume} needs ${gb(bytes)} and has ${gb(free)} free`);
    else fine.push(`${gb(free)} free on ${volume}`);
  }
  const p = $('#wiz-review-room');
  p.className = `small-print ${short.length ? 'err' : 'muted'}`;
  p.textContent = short.length
    ? `Not enough room: ${short.join('; ')}. Go back and choose a smaller library or model, or a folder on a bigger drive.`
    : `There is room: ${fine.join(', ')}. At 5 MB a second (a 40 megabit connection) this is about ${duration(total / 5e6)}; you can leave it running.`;
  $('#wiz-go').disabled = short.length > 0;
}

function duration(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 90) return `${minutes} minute${minutes === 1 ? '' : 's'}`;
  return `${Math.round(minutes / 60)} hours`;
}

const STAGES = [
  ['tools', 'Helper programs'],
  ['models', 'Models'],
  ['library', 'Library'],
  ['fit', 'Measure the model against this computer'],
  ['services', 'Start the model'],
  ['index', 'Build the search index'],
  ['home', 'Your home area'],
];

function resetRun() {
  $('#wiz-stages').hidden = true;
  $('#wiz-progress').hidden = true;
  $('#wiz-go').textContent = 'Set up this computer';
  $('#wiz-go').disabled = false;
}

/** Marks a stage 'doing', 'done', 'skipped' or 'failed'. */
function stage(id, state) {
  const li = $(`#wiz-stages li[data-stage="${id}"]`);
  if (!li) return;
  li.className = state;
  li.querySelector('.tick').textContent = { doing: '…', done: '✓', skipped: '–', failed: '✗' }[state] ?? '';
}

/** A model's name as the person chose it, for an id in a progress event. */
function titleOf(id) {
  if (id === plan?.reranker?.id) return plan.reranker.title;
  return chooser.data()?.models.find((m) => m.id === id)?.title ?? id;
}

function showProgress(label, frac) {
  $('#wiz-progress').hidden = false;
  $('#wiz-label').textContent = label;
  $('#wiz-fill').style.width = frac === null ? '100%' : `${Math.round(frac * 100)}%`;
}

listen('fetch-progress', (e) => {
  if (!running) return;
  const p = e.payload;
  const frac = p.total > 0 ? p.received / p.total : null;
  const size = p.total > 0 ? ` ${gb(p.received)} of ${gb(p.total)}` : '';
  showProgress(`${titleOf(p.item)}${p.note ? ` — ${p.note}` : ''}${size}`, frac);
});
listen('service-progress', (e) => {
  if (running) showProgress(String(e.payload), null);
});
listen('index-progress', (e) => {
  if (running) showProgress(String(e.payload), null);
});

/**
 * The whole first run, in order. Each step is resumable and skips what is
 * already present, so a failure partway through costs a retry, not a restart.
 */
$('#wiz-go').addEventListener('click', async () => {
  if (running) return;
  const f = folders();
  const model = chooser.chosen();
  const small = spare() && $('#wiz-spare').checked ? spare() : null;
  const bundle = $('#wiz-bundle').value;
  const bundleTitle = plan.bundles.find((b) => b.id === bundle)?.title ?? bundle;
  const btn = $('#wiz-go');
  running = true;
  btn.disabled = true;
  $('#wiz-back').disabled = true;
  const list = $('#wiz-stages');
  list.textContent = '';
  for (const [id, name] of STAGES) {
    const li = el('li');
    li.dataset.stage = id;
    li.append(el('span', 'tick', ''), el('span', undefined, name));
    list.append(li);
  }
  list.hidden = false;
  note('');
  // The list of downloads fills the window; what is happening is below it.
  list.scrollIntoView({ block: 'start', behavior: 'smooth' });
  let at = '';
  const begin = (id, label) => {
    at = id;
    stage(id, 'doing');
    showProgress(label, null);
  };
  try {
    const current = await invoke('get_config');
    await invoke('set_config', { config: { ...current, library_dir: f.library } });

    begin('tools', 'Fetching helper programs…');
    await invoke('fetch_tools', { dest: f.tools });
    stage('tools', plan.tools.length ? 'done' : 'skipped');

    begin('models', 'Fetching models…');
    if (plan.reranker) {
      showProgress(`Fetching ${plan.reranker.title}…`, 0);
      await invoke('fetch_model', { id: plan.reranker.id, dest: f.models });
    }
    // The spare before the chosen model: the last one fetched is the one
    // Museion is set to use.
    if (small) {
      showProgress(`Fetching ${small.title}…`, 0);
      await invoke('fetch_model', { id: small.id, dest: f.models });
    }
    if (model) {
      showProgress(`Fetching ${model.title}…`, 0);
      await invoke('fetch_model', { id: model.id, dest: f.models });
    }
    stage('models', 'done');

    begin('library', `Fetching the ${bundleTitle} library…`);
    await invoke('fetch_bundle', { id: bundle });
    stage('library', 'done');

    // Fit before anything is loaded. The fit measures free GPU memory, and
    // with the chat model already resident it reads the card as full and
    // returns a split far more cautious than the machine deserves.
    const data = chooser.data();
    const fitWanted = model && model.id !== data.spare && plan.hardware.memory_mb > 0;
    if (fitWanted) {
      begin('fit', 'Measuring the model against this graphics card…');
      await invoke('fit_chat_model');
      stage('fit', 'done');
    } else {
      stage('fit', 'skipped');
    }

    begin('services', 'Starting the model — a large one takes a few minutes to load…');
    await invoke('start_services');
    stage('services', 'done');

    begin('index', 'Building the search index…');
    await invoke('build_index', { force: false });
    stage('index', 'done');

    // Last, and not a reason to stop: everything else works without it,
    // and Settings can do it again.
    let homeTrouble = '';
    if (home.wanted()) {
      begin('home', 'Your home area…');
      try {
        await home.run((t) => showProgress(`Your home area: ${t}`, null));
        stage('home', 'done');
      } catch (e) {
        homeTrouble = String(e);
        stage('home', 'failed');
      }
    } else {
      stage('home', 'skipped');
    }

    statusMsg('Set-up complete.');
    await refreshSetup();
    await refreshServiceDots();
    $('#wiz-done-note').textContent = homeTrouble
      ? `Everything is in place, but your home area was not set up: ${homeTrouble} You can set it up in Settings.`
      : `${model ? `${model.title} is loaded` : 'Museion is set up'} and the ${bundleTitle} library is indexed.`;
    show('done');
  } catch (e) {
    if (at) stage(at, 'failed');
    showProgress(`Stopped: ${String(e)}`, null);
    note('Nothing downloaded is lost. Fix the problem if you can, then try again.', true);
    statusMsg(String(e), true);
    btn.textContent = 'Try again';
  } finally {
    running = false;
    btn.disabled = false;
    $('#wiz-back').disabled = false;
  }
});

$('#wiz-done').addEventListener('click', () => leaveWelcome('ask'));

$('#welcome-skip').addEventListener('click', (e) => {
  e.preventDefault();
  leaveWelcome('setup');
});

onTab('welcome', loadPlan);
onTab('welcome', home.refresh);
