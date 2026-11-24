// The model chooser: this computer in plain words, and each chat model with
// whether it fits, how big it is, and what it can and cannot do. The facts
// come from museion/model_choice.py; the words about each model are in its
// file under models/. First-run set-up uses it to pick what to fetch
// (welcome.js); Settings uses it to change model later.

import { invoke, listen } from './api.js';
import { $, el, gb, statusMsg } from './dom.js';
import { refreshServiceDots } from './dots.js';

function list(cls, mark, items) {
  const ul = el('ul', `points ${cls}`);
  for (const text of items) {
    const li = el('li');
    li.append(el('span', 'tick', mark), el('span', undefined, text));
    ul.append(li);
  }
  return ul;
}

function agentsLine(m) {
  const all = m.helpers_run.length + m.helpers_not.length;
  if (!all) return '';
  if (!m.helpers_not.length) return `Runs all ${all} agents.`;
  return `Runs ${m.helpers_run.length} of the ${all} agents (${m.helpers_run.join(', ')}). `
    + `Not: ${m.helpers_not.join(', ')}.`;
}

/**
 * A chooser inside `root`.
 *   use: false  a radio list; chosen() is the model picked (set-up)
 *   use: true   each model has a button that fetches and switches to it (Settings)
 * onChange is called when the pick changes. Returns { refresh(dir), chosen(), data() }.
 */
export function modelChooser(root, { use = false, onChange = () => {} } = {}) {
  let data = null;
  let picked = null; // a model id
  let busy = false;
  const name = `model-pick-${Math.random().toString(36).slice(2, 8)}`;

  const machine = el('div', 'model-machine');
  const cards = el('div', 'model-cards');
  const status = el('p', 'small-print model-status');
  root.textContent = '';
  root.append(machine, cards, status);

  const say = (text, cls = '') => {
    status.textContent = text;
    status.className = `small-print model-status ${cls}`;
  };

  function badge(text, cls) {
    return el('span', `badge ${cls}`, text);
  }

  function card(m) {
    const box = el(use ? 'div' : 'label', 'model-choice');
    if (m.id === picked && !use) box.classList.add('chosen');
    if (!m.fits) box.classList.add('too-big');

    const head = el('div', 'model-head');
    if (!use) {
      const radio = el('input');
      radio.type = 'radio';
      radio.name = name;
      radio.value = m.id;
      radio.checked = m.id === picked;
      radio.disabled = m.no_room;
      radio.addEventListener('change', () => {
        picked = m.id;
        draw();
        onChange();
      });
      head.append(radio);
    }
    head.append(el('span', 'model-title', m.title));
    head.append(el('span', 'model-size', m.on_disk ? `${gb(m.size_bytes)}, already downloaded` : `${gb(m.size_bytes)} to download`));
    box.append(head);

    const badges = el('div', 'badges');
    if (m.recommended) badges.append(badge('Recommended for this computer', 'good'));
    else if (m.fits) badges.append(badge('Fits this computer', 'ok'));
    else badges.append(badge('Too big for this computer', 'bad'));
    if (m.current) badges.append(badge('In use now', 'ok'));
    box.append(badges);

    if (m.summary) box.append(el('p', 'model-summary', m.summary));
    if (m.why_not) box.append(el('p', 'model-warn', m.why_not));
    if (m.no_room) {
      box.append(el('p', 'model-warn', `Not enough room in the models folder: it needs ${gb(m.size_bytes)} `
        + `and ${gb(data.free_bytes)} is free. Choose a folder on a bigger drive.`));
    }
    if (m.good_for.length) box.append(list('good', '✓', m.good_for));
    if (m.limits.length) box.append(list('limits', '✗', m.limits));
    // Which agents it runs is only true of a model that runs here at all.
    const facts = [m.fits ? agentsLine(m) : '', m.speed ? `Speed: ${m.speed}` : '', `Needs ${m.needs}.`].filter(Boolean);
    box.append(el('p', 'model-facts', facts.join(' ')));

    if (use) {
      const go = el('button', m.recommended && !m.current ? 'primary small' : 'small',
        m.current ? 'In use' : m.on_disk ? 'Use this model' : `Download (${gb(m.size_bytes)}) and use`);
      go.type = 'button';
      go.disabled = busy || m.current || m.no_room;
      go.addEventListener('click', () => void useModel(m));
      box.append(go);
    }
    return box;
  }

  function draw() {
    machine.textContent = '';
    cards.textContent = '';
    if (!data) return;
    machine.append(el('div', 'model-machine-title', 'This computer'));
    const ul = el('ul');
    for (const line of data.machine) ul.append(el('li', undefined, line));
    if (data.free_bytes !== null && data.models_dir) {
      ul.append(el('li', undefined, `${gb(data.free_bytes)} free where models are kept (${data.models_dir})`));
    }
    machine.append(ul);
    for (const m of data.models) cards.append(card(m));
  }

  async function refresh(dir = null) {
    try {
      data = await invoke('model_choices', dir ? { models_dir: dir } : {});
    } catch (e) {
      say(String(e), 'err');
      return;
    }
    const ok = (id) => data.models.some((m) => m.id === id && !m.no_room);
    if (!ok(picked)) picked = ok(data.recommended) ? data.recommended : (data.models.find((m) => m.fits && !m.no_room)?.id ?? null);
    draw();
    onChange();
  }

  async function useModel(m) {
    if (!m.fits && !confirm(`${m.title} is too big for this computer. ${m.why_not} Use it anyway?`)) return;
    busy = true;
    draw();
    say(m.on_disk ? `Switching to ${m.title}…` : `Downloading ${m.title}…`, 'muted');
    const offFetch = listen('fetch-progress', (e) => {
      const p = e.payload;
      if (p.item !== m.id || !(p.total > 0)) return;
      say(`Downloading ${m.title}: ${gb(p.received)} of ${gb(p.total)} (${Math.floor((100 * p.received) / p.total)}%)`, 'muted');
    });
    const offService = listen('service-progress', (e) => say(e.payload, 'muted'));
    try {
      const done = await invoke('use_model', { id: m.id });
      say(done, 'good');
      statusMsg(done);
    } catch (e) {
      say(String(e), 'err');
    } finally {
      offFetch();
      offService();
      busy = false;
      await refresh();
      $('#chat-model').value = (await invoke('get_config').catch(() => ({}))).chat_model ?? $('#chat-model').value;
      await refreshServiceDots();
    }
  }

  return {
    refresh,
    chosen: () => data?.models.find((m) => m.id === picked) ?? null,
    data: () => data,
  };
}

// Settings › Models: the chooser opens under "Help me choose".
const guide = $('#model-guide');
if (guide) {
  const chooser = modelChooser(guide, { use: true });
  $('#model-guide-open').addEventListener('click', async () => {
    guide.hidden = !guide.hidden;
    $('#model-guide-open').textContent = guide.hidden ? 'Help me choose a model…' : 'Hide the model guide';
    if (!guide.hidden) await chooser.refresh();
  });
}
