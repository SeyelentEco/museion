// Settings › Prepare a drive, and checking the drive Museion runs from.
// The work is in museion/prepare.py; this only gathers the choices and
// shows the progress.

import { invoke, listen } from './api.js';
import { pickInto } from './chooser.js';
import { $, el, gb } from './dom.js';
import { onTab } from './tabs.js';

let options = null;
let preparing = false;

function plan() {
  const dest = $('#prep-dest').value.trim();
  if (!options || !dest) return null;
  const checked = (sel) => [...document.querySelectorAll(`${sel} input:checked`)].map((i) => i.value);
  return {
    dest,
    bundle: $('#prep-bundle').value,
    models: checked('#prep-models'),
    accelerators: checked('#prep-accels'),
    opencode: $('#prep-opencode').checked,
  };
}

async function refreshPrepare() {
  const st = await invoke('get_status').catch(() => null);
  $('#card-drive').hidden = !st?.portable_root;
  if (options) return;
  try {
    options = await invoke('prepare_options');
  } catch (e) {
    $('#prep-estimate').textContent = String(e);
    $('#prep-estimate').classList.add('err');
    return;
  }
  const sel = $('#prep-bundle');
  sel.textContent = '';
  for (const b of options.bundles) {
    const o = el('option', undefined, `${b.title} (${gb(b.size_bytes)})`);
    o.value = b.id;
    sel.append(o);
  }
  const check = (host, value, label, on) => {
    const l = el('label', 'check');
    const i = el('input');
    i.type = 'checkbox';
    i.value = value;
    i.checked = on;
    l.append(i, document.createTextNode(' ' + label));
    host.append(l);
    i.addEventListener('change', () => void estimate());
  };
  const models = $('#prep-models');
  models.textContent = '';
  for (const m of options.models) {
    check(models, m.id, `${m.title} (${gb(m.size_bytes)}, ${m.tiers.join('/')})`, m.tiers.includes('Modest'));
  }
  const accels = $('#prep-accels');
  accels.textContent = '';
  for (const a of options.accelerators) check(accels, a, a.toUpperCase(), true);
  if (options.opencode_bytes) $('#prep-opencode-label').textContent = `Carry opencode (${gb(options.opencode_bytes)})`;
  void estimate();
}

async function estimate() {
  const p = plan();
  const out = $('#prep-estimate');
  const btn = $('#prep-start');
  out.classList.remove('err');
  if (!p) {
    out.textContent = 'Choose a destination to see the size.';
    btn.setAttribute('disabled', 'true');
    return;
  }
  try {
    const e = await invoke('prepare_estimate', { plan: p });
    const fits = e.free_bytes >= e.remaining + 1024 ** 3;
    out.textContent = `${gb(e.bytes)} in all; ${gb(e.remaining)} to fetch; ${gb(e.free_bytes)} free at the destination.${fits ? '' : ' That will not fit.'}`;
    if (fits && !preparing) btn.removeAttribute('disabled');
    else btn.setAttribute('disabled', 'true');
  } catch (err) {
    out.textContent = String(err);
    out.classList.add('err');
    btn.setAttribute('disabled', 'true');
  }
}

$('#prep-bundle').addEventListener('change', () => void estimate());
$('#prep-dest').addEventListener('change', () => void estimate());
$('#prep-opencode').addEventListener('change', () => void estimate());
$('#prep-pick').addEventListener('click', async () => {
  await pickInto('#prep-dest', true, 'Where the drive is');
  void estimate();
});

listen('prepare-progress', (e) => {
  if (!preparing) return;
  const p = e.payload;
  const pct = p.total > 0 && p.received <= p.total ? ` ${Math.floor((p.received / p.total) * 100)}%` : '';
  $('#prep-progress').textContent = p.item ? `${p.phase}: ${p.item}${pct}` : p.phase;
});

$('#prep-start').addEventListener('click', async () => {
  const p = plan();
  if (!p) return;
  const btn = $('#prep-start');
  const out = $('#prep-progress');
  preparing = true;
  btn.setAttribute('disabled', 'true');
  out.classList.remove('err');
  out.textContent = 'Starting…';
  try {
    const root = await invoke('prepare_drive', { plan: p });
    out.textContent = `Ready. The drive at ${root} runs Museion on any computer: open the Museion launcher on it, and read README.txt there.`;
  } catch (err) {
    out.textContent = `Stopped: ${String(err)} Prepare again to carry on where it stopped.`;
    out.classList.add('err');
  } finally {
    preparing = false;
    void estimate();
  }
});

let verifying = false;
listen('verify-progress', (e) => {
  if (verifying) $('#drive-verify-status').textContent = `${e.payload.item} (${e.payload.received} of ${e.payload.total})`;
});

$('#drive-verify').addEventListener('click', async () => {
  const btn = $('#drive-verify');
  const out = $('#drive-verify-status');
  verifying = true;
  btn.setAttribute('disabled', 'true');
  out.classList.remove('err');
  out.textContent = 'Checking…';
  try {
    out.textContent = await invoke('verify_drive');
  } catch (err) {
    out.textContent = String(err);
    out.classList.add('err');
  } finally {
    verifying = false;
    btn.removeAttribute('disabled');
  }
});

onTab('setup', refreshPrepare);
