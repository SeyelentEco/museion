// The Evals tab: run the benchmark against a model, and compare two runs.

import { invoke, listen } from './api.js';
import { renderAnswer } from './answer.js';
import { chooseFile } from './chooser.js';
import { $, el, gb } from './dom.js';
import { refreshServiceDots } from './dots.js';
import { onTab } from './tabs.js';

const CAP_NAMES = {
  retrieve_pdf: 'Retrieve from PDFs',
  retrieve_article: 'Retrieve from articles',
  calculate: 'Calculate',
  reason: 'Reason',
  plan: 'Plan',
  refuse: 'Refuse / label',
};
const HORIZON_NAMES = {
  month: 'First month',
  six_months: 'Six months',
  year: 'A year',
  five_years: 'Five years',
  any: 'Any time',
};

let evalRuns = [];
let evalRunning = false;

/** Fill the model picker: the models folder's GGUFs, current one first. */
async function refreshEvalModels(extra) {
  const sel = $('#eval-model');
  const keep = extra ?? sel.value;
  let models = [];
  try {
    models = await invoke('list_models');
  } catch {
    /* no models folder yet */
  }
  sel.textContent = '';
  for (const m of models) {
    const o = el('option', undefined, `${m.name} (${gb(m.size_bytes)})${m.current ? ' — configured' : ''}`);
    o.value = m.path;
    sel.append(o);
  }
  if (extra && !models.some((m) => m.path === extra)) {
    const o = el('option', undefined, extra.replace(/^.*[\\/]/, ''));
    o.value = extra;
    sel.append(o);
  }
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

$('#eval-pick-model').addEventListener('click', async () => {
  const chosen = await chooseFile({ title: 'Choose a chat model (.gguf)', want: 'gguf' });
  if (chosen) await refreshEvalModels(chosen);
});

function runName(r) {
  const d = new Date(Number(r.id) * 1000);
  const when = isNaN(d.getTime()) ? r.id : d.toLocaleString();
  return `${when} — ${r.label || r.model} — ${r.passed}/${r.total}`;
}

async function refreshEvals() {
  void refreshEvalModels();
  try {
    evalRuns = await invoke('list_eval_runs');
  } catch (e) {
    $('#eval-summary').textContent = String(e);
    return;
  }
  const a = $('#eval-a');
  const b = $('#eval-b');
  const keepA = a.value;
  const keepB = b.value;
  a.textContent = '';
  b.textContent = '';
  const none = el('option', undefined, '— no comparison —');
  none.value = '';
  b.append(none);
  for (const r of evalRuns) {
    for (const sel of [a, b]) {
      const o = el('option', undefined, runName(r));
      o.value = r.id;
      sel.append(o);
    }
  }
  if (evalRuns.length) {
    a.value = evalRuns.some((r) => r.id === keepA) ? keepA : evalRuns[0].id;
    b.value = evalRuns.some((r) => r.id === keepB) ? keepB : '';
  }
  renderEvalSummary();
}

function pct(p, t) {
  return t ? `${Math.round((p / t) * 100)}%` : '—';
}

function cell(tag, text, cls) {
  return el(tag, cls, text);
}

/** A small table by capability or horizon, with run B alongside if chosen. */
function tallyTable(title, rows, other, names) {
  const t = el('table');
  const tr = el('tr');
  for (const h of [title, 'A', ...(other ? ['B', 'Δ'] : [])]) tr.append(cell('th', h));
  t.append(tr);
  for (const [k, p, n] of rows) {
    const r = el('tr');
    r.append(cell('td', names[k] ?? k), cell('td', `${p}/${n} (${pct(p, n)})`, 'num'));
    if (other) {
      const o = other.find((x) => x[0] === k);
      r.append(cell('td', o ? `${o[1]}/${o[2]}` : '—', 'num'));
      const c4 = cell('td', '', 'num');
      if (o) {
        const d = p - o[1];
        c4.textContent = d === 0 ? '=' : d > 0 ? `+${d}` : `${d}`;
        // classList.add('') throws, and took the whole case table with it
        // whenever a delta was exactly zero.
        if (d !== 0) c4.classList.add(d > 0 ? 'better' : 'worse');
      }
      r.append(c4);
    }
    t.append(r);
  }
  const w = el('div', 'tablewrap');
  w.append(t);
  return w;
}

function renderEvalSummary() {
  const box = $('#eval-summary');
  box.textContent = '';
  const A = evalRuns.find((r) => r.id === $('#eval-a').value);
  const B = evalRuns.find((r) => r.id === $('#eval-b').value);
  if (!A) {
    box.append(el('p', 'muted', 'No runs yet. Run the benchmark above.'));
    return;
  }

  box.append(el('div', 'headline', `${A.passed}/${A.total} passed`));
  const vs = B ? `   vs   ${B.model}: ${B.passed}/${B.total}` : '';
  box.append(el('div', 'sub', `${A.model}${A.reranker ? ' · reranker on' : ' · no reranker'} · ${Math.round(A.seconds)}s${vs}`));

  const grid = el('div', 'grid');
  grid.append(tallyTable('Capability', A.by_capability, B?.by_capability, CAP_NAMES));
  grid.append(tallyTable('Horizon', A.by_horizon, B?.by_horizon, HORIZON_NAMES));
  box.append(grid);

  // Every case, with the comparison column; click a row for the answer.
  const t = el('table');
  const hr = el('tr');
  for (const h of ['Case', 'Capability', 'Horizon', 'A', ...(B ? ['B'] : []), 's']) hr.append(cell('th', h));
  t.append(hr);
  for (const c of A.cases) {
    const r = el('tr', 'case');
    r.append(cell('td', c.id), cell('td', CAP_NAMES[c.capability] ?? c.capability));
    r.append(cell('td', HORIZON_NAMES[c.horizon] ?? c.horizon));
    r.append(cell('td', c.passed ? 'pass' : 'fail', c.passed ? 'pass' : 'fail'));
    const bc = B?.cases.find((x) => x.id === c.id);
    if (B) r.append(cell('td', bc ? (bc.passed ? 'pass' : 'fail') : '—', bc ? (bc.passed ? 'pass' : 'fail') : ''));
    r.append(cell('td', c.seconds.toFixed(0), 'num'));
    r.addEventListener('click', () => showEvalCase(c, bc));
    t.append(r);
  }
  // The case table is the wide one; it scrolls inside its own box rather
  // than dragging the whole page sideways.
  const wrap = el('div', 'tablewrap');
  wrap.append(t);
  box.append(wrap);
}

function showEvalCase(c, other) {
  const card = $('#eval-detail-card');
  const box = $('#eval-detail');
  card.hidden = false;
  $('#eval-detail-title').textContent = `${c.id}: ${c.prompt}`;
  box.textContent = '';
  const one = (r, name) => {
    const w = el('div');
    w.append(el('div', r.passed ? 'pass' : 'fail', `${name}: ${r.passed ? 'pass' : 'fail'} · basis ${r.basis || '—'} · ${r.seconds.toFixed(0)}s`));
    for (const ch of r.checks) {
      w.append(el('div', `check ${ch.passed ? 'pass' : 'fail'}`, `${ch.passed ? '✓' : '✗'} ${ch.check}: ${ch.detail}`));
    }
    for (const items of [r.calculations, r.sources]) {
      if (!items.length) continue;
      const ul = el('ul', 'plain');
      for (const x of items) ul.append(el('li', undefined, x));
      w.append(ul);
    }
    const ans = el('div', 'answer-box');
    renderAnswer(ans, r.error ?? r.answer);
    w.append(ans);
    return w;
  };
  if (other) {
    const g = el('div', 'grid');
    g.append(one(c, 'A'), one(other, 'B'));
    box.append(g);
  } else {
    box.append(one(c, 'A'));
  }
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('#eval-a').addEventListener('change', renderEvalSummary);
$('#eval-b').addEventListener('change', renderEvalSummary);

listen('eval-progress', (e) => {
  const p = e.payload;
  $('#eval-fill').style.width = `${Math.round(((p.passed === null ? p.index - 1 : p.index) / p.total) * 100)}%`;
  $('#eval-status').textContent =
    p.passed === null ? `${p.index}/${p.total}  ${p.id}…` : `${p.index}/${p.total}  ${p.id}: ${p.passed ? 'pass' : 'fail'}`;
});

$('#eval-run').addEventListener('click', async () => {
  if (evalRunning) return;
  evalRunning = true;
  const btn = $('#eval-run');
  btn.setAttribute('disabled', 'true');
  $('#eval-progress').hidden = false;
  $('#eval-status').textContent = 'Starting…';
  $('#eval-fill').style.width = '0%';
  // Model swaps narrate through the same channel the services use.
  const offProgress = listen('service-progress', (e) => {
    $('#eval-status').textContent = e.payload;
  });
  try {
    const model = $('#eval-model').value;
    const modelName = model.replace(/^.*[\\/]/, '').replace(/\.gguf$/i, '');
    const label = $('#eval-label').value.trim() || modelName;
    const only = $('#eval-only').value;
    const run = await invoke('run_evals', { label, only, model });
    $('#eval-status').textContent = `Done: ${run.passed}/${run.total} passed in ${Math.round(run.seconds)}s.`;
    $('#eval-fill').style.width = '100%';
    await refreshEvals();
    $('#eval-a').value = run.id;
    renderEvalSummary();
  } catch (e) {
    $('#eval-status').textContent = `Stopped: ${String(e)}`;
  } finally {
    offProgress();
    evalRunning = false;
    btn.removeAttribute('disabled');
    await refreshServiceDots();
  }
});

onTab('evals', refreshEvals);
