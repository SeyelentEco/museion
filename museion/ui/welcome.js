// The first-run screen: choose three folders, see what this machine would
// fetch and how big it is, then fetch it all and get ready to answer.

import { invoke, listen } from './api.js';
import { pickInto } from './chooser.js';
import { $, el, gb, statusMsg } from './dom.js';
import { refreshServiceDots } from './dots.js';
import { refreshSetup } from './settings.js';
import { leaveWelcome, onTab } from './tabs.js';

let plan = null;

async function loadPlan() {
  const dl = $('#wiz-plan');
  plan = null;
  dl.textContent = 'Looking at this machine…';
  // The folders first, from Settings: filled in after the plan, they would
  // overwrite whatever the person had typed while it loaded.
  try {
    const cfg = await invoke('get_config');
    $('#wiz-library').value = cfg.library_dir ?? '';
    $('#wiz-models').value = cfg.models_dir ?? '';
    $('#wiz-tools').value = cfg.kiwix_exe?.replace(/[\\/][^\\/]*$/, '') ?? '';
  } catch (e) {
    dl.textContent = String(e);
    return;
  }
  try {
    plan = await invoke('setup_plan');
  } catch (e) {
    dl.textContent = String(e);
    return;
  }
  dl.textContent = '';
  const add = (k, v) => {
    dl.append(el('dt', undefined, k));
    dl.append(el('dd', undefined, v));
  };
  const item = (p) => `${p.title} (${gb(p.size_bytes)})`;
  add('This machine', `${plan.hardware.accelerator} — ${plan.hardware.tier} tier`);
  add('Helper programs', plan.tools.map(item).join(', ') || 'none for this system in the catalogue');
  add('Reranker', plan.reranker ? item(plan.reranker) : '—');
  add('Chat model', plan.chat_model ? item(plan.chat_model) : 'none fits this machine');

  const sel = $('#wiz-bundle');
  sel.textContent = '';
  for (const b of plan.bundles) {
    const o = el('option', undefined, `${b.title} — ${gb(b.size_bytes)}`);
    o.value = b.id;
    if (b.id === 'go-bag') o.selected = true;
    sel.append(o);
  }
}

function showProgress(label, frac) {
  $('#wiz-progress').hidden = false;
  $('#wiz-label').textContent = label;
  $('#wiz-fill').style.width = frac === null ? '100%' : `${Math.round(frac * 100)}%`;
}

listen('fetch-progress', (e) => {
  if (!$('#panel-welcome').classList.contains('active')) return;
  const p = e.payload;
  const frac = p.total > 0 ? p.received / p.total : null;
  const size = p.total > 0 ? ` ${gb(p.received)} / ${gb(p.total)}` : '';
  showProgress(`${p.item}${p.note ? ` — ${p.note}` : ''}${size}`, frac);
});

$('#wiz-pick-library').addEventListener('click', () => pickInto('#wiz-library', true, 'Folder for the library'));
$('#wiz-pick-models').addEventListener('click', () => pickInto('#wiz-models', true, 'Folder for models'));
$('#wiz-pick-tools').addEventListener('click', () => pickInto('#wiz-tools', true, 'Folder for helper programs'));

/**
 * The whole first run, in order: programs, reranker, chat model, content,
 * fit, services, index. Each step is resumable and skips what is already
 * present, so a failure partway through costs a retry, not a restart.
 */
$('#wiz-go').addEventListener('click', async () => {
  if (!plan) {
    showProgress('Still looking at this machine; try again in a moment.', null);
    return;
  }
  const lib = $('#wiz-library').value.trim();
  const models = $('#wiz-models').value.trim();
  const tools = $('#wiz-tools').value.trim();
  if (!lib || !models || !tools) {
    showProgress('Choose all three folders first.', null);
    return;
  }
  const btn = $('#wiz-go');
  btn.setAttribute('disabled', 'true');
  try {
    const current = await invoke('get_config');
    await invoke('set_config', { config: { ...current, library_dir: lib } });

    showProgress('Fetching helper programs…', null);
    await invoke('fetch_tools', { dest: tools });

    if (plan.reranker) {
      showProgress(`Fetching ${plan.reranker.title}…`, 0);
      await invoke('fetch_model', { id: plan.reranker.id, dest: models });
    }
    if (plan.chat_model) {
      showProgress(`Fetching ${plan.chat_model.title}…`, 0);
      await invoke('fetch_model', { id: plan.chat_model.id, dest: models });
    }

    const bundle = $('#wiz-bundle').value;
    showProgress(`Fetching ${bundle}…`, 0);
    await invoke('fetch_bundle', { id: bundle });

    // Fit before anything is loaded. The fit measures free GPU memory, and
    // with the chat model already resident it reads the card as full and
    // returns a split far more cautious than the machine deserves.
    if (plan.chat_model && plan.hardware.tier !== 'Modest') {
      showProgress('Fitting the model to this GPU…', null);
      await invoke('fit_chat_model');
    }

    showProgress('Starting services…', null);
    await invoke('start_services');

    showProgress('Building the index…', null);
    await invoke('build_index', { force: false });

    showProgress('Ready. Ask something.', 1);
    statusMsg('Set-up complete.');
    await refreshSetup();
    await refreshServiceDots();
    // Done: the set-up screen has served its purpose.
    setTimeout(() => leaveWelcome('ask'), 1200);
  } catch (e) {
    showProgress(`Stopped: ${String(e)}`, null);
    statusMsg(String(e), true);
  } finally {
    btn.removeAttribute('disabled');
  }
});

$('#welcome-skip').addEventListener('click', (e) => {
  e.preventDefault();
  leaveWelcome('setup');
});

onTab('welcome', loadPlan);
