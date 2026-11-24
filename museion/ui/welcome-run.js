// The last step of first-run set-up: the list of what will be downloaded,
// whether it fits, and then the run itself -- programs, reranker, chat model,
// content, fit, services, index, home area -- as it always was: each part
// resumable, so a failure partway costs a retry, not a restart.
//
// welcome.js owns the steps before this one and hands over what they chose.

import { invoke, listen } from './api.js';
import { $, el, gb, statusMsg } from './dom.js';
import { refreshServiceDots } from './dots.js';
import { refreshSetup } from './settings.js';

// What your home area downloads, for the list of downloads (home.js says the same).
const HOME_BYTES = 150 * 1048576;

/**
 * `w` is the wizard: plan() and room() as they stand, the model chooser,
 * the home-area form, folders(), spare(), showRoom(), show(step), note().
 * Returns { showReview, reset, running }.
 */
export function reviewAndRun(w) {
  const { plan, room, chooser, home, folders, spare, showRoom, show, note } = w;
  let busy = false;

  /** Everything set-up will fetch: { title, bytes, where } with `where` an
   * index into `room` (library, models, helper programs). */
  function items() {
    const out = [];
    // A release or a prepared drive already carries its helper programs.
    if (!plan().portable) for (const t of plan().tools) out.push({ title: t.title, what: 'Helper program', bytes: t.size_bytes, where: 2 });
    if (plan().reranker) {
      const have = chooser.data()?.reranker_on_disk;
      out.push({ title: plan().reranker.title, what: 'Puts the best passages first', bytes: have ? 0 : plan().reranker.size_bytes, where: 1, have });
    }
    const model = chooser.chosen();
    if (model) out.push({ title: model.title, what: 'The model that writes the answers', bytes: model.on_disk ? 0 : model.size_bytes, where: 1, have: model.on_disk });
    if (spare() && $('#wiz-spare').checked) out.push({ title: spare().title, what: 'Spare model for any computer', bytes: spare().size_bytes, where: 1 });
    const bundle = plan().bundles.find((b) => b.id === $('#wiz-bundle').value);
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
      const r = room()[it.where];
      if (r) need.set(r.volume, (need.get(r.volume) ?? 0) + it.bytes);
    }
    const short = [];
    const fine = [];
    for (const [volume, bytes] of need) {
      const free = room().find((r) => r.volume === volume)?.free_bytes ?? 0;
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

  function reset() {
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
    if (id === plan()?.reranker?.id) return plan().reranker.title;
    return chooser.data()?.models.find((m) => m.id === id)?.title ?? id;
  }

  function showProgress(label, frac) {
    $('#wiz-progress').hidden = false;
    $('#wiz-label').textContent = label;
    $('#wiz-fill').style.width = frac === null ? '100%' : `${Math.round(frac * 100)}%`;
  }

  listen('fetch-progress', (e) => {
    if (!busy) return;
    const p = e.payload;
    const frac = p.total > 0 ? p.received / p.total : null;
    const size = p.total > 0 ? ` ${gb(p.received)} of ${gb(p.total)}` : '';
    showProgress(`${titleOf(p.item)}${p.note ? ` — ${p.note}` : ''}${size}`, frac);
  });
  listen('service-progress', (e) => {
    if (busy) showProgress(String(e.payload), null);
  });
  listen('index-progress', (e) => {
    if (busy) showProgress(String(e.payload), null);
  });

  /**
   * The whole first run, in order. Each step is resumable and skips what is
   * already present, so a failure partway through costs a retry, not a restart.
   */
  $('#wiz-go').addEventListener('click', async () => {
    if (busy) return;
    const f = folders();
    const model = chooser.chosen();
    const small = spare() && $('#wiz-spare').checked ? spare() : null;
    const bundle = $('#wiz-bundle').value;
    const bundleTitle = plan().bundles.find((b) => b.id === bundle)?.title ?? bundle;
    const btn = $('#wiz-go');
    busy = true;
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
      stage('tools', plan().tools.length && !plan().portable ? 'done' : 'skipped');

      begin('models', 'Fetching models…');
      if (plan().reranker) {
        showProgress(`Fetching ${plan().reranker.title}…`, 0);
        await invoke('fetch_model', { id: plan().reranker.id, dest: f.models });
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
      const fitWanted = model && model.id !== data.spare && plan().hardware.memory_mb > 0;
      let fitTrouble = '';
      if (fitWanted) {
        begin('fit', 'Measuring the model against this graphics card…');
        // Not a reason to stop: the model may still load as it is, and
        // Settings › Fit model can measure it again.
        try {
          await invoke('fit_chat_model');
          stage('fit', 'done');
        } catch (e) {
          fitTrouble = String(e);
          stage('fit', 'failed');
        }
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
      $('#wiz-done-note').textContent = fitTrouble
        ? `Everything is in place, but the model could not be measured against the graphics card (${fitTrouble}), so it may be slower than it should be. Settings › Fit model tries again.`
        : homeTrouble
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
      busy = false;
      btn.disabled = false;
      $('#wiz-back').disabled = false;
    }
  });

  return { showReview, reset, running: () => busy };
}
