// The Settings tab: this machine, where things are, the helpers, the extra
// content folders, maintenance, and the prompts-and-limits file.

import { invoke, listen } from './api.js';
import { pickInto, chooseFile } from './chooser.js';
import { $, el, fmt, gb, statusMsg } from './dom.js';
import { refreshServiceDots, setDot } from './dots.js';
import { onTab, showWelcome } from './tabs.js';

export async function refreshSetup() {
  try {
    const hw = await invoke('get_hardware');
    const dl = $('#hardware');
    dl.textContent = '';
    const add = (k, v) => {
      dl.append(el('dt', undefined, k));
      dl.append(el('dd', undefined, v));
    };
    add('Accelerator', hw.accelerator);
    if (hw.memory_mb > 0) add('Memory', `${fmt(hw.memory_mb)} MB`);
    add('System RAM', `${fmt(hw.system_ram_mb)} MB`);
    add('Tier', `${hw.tier} — ${hw.tier_detail}`);
  } catch (e) {
    $('#hardware').textContent = String(e);
  }

  const cfg = await invoke('get_config');
  $('#library-dir').value = cfg.library_dir ?? '';
  $('#chat-model').value = cfg.chat_model ?? '';
  $('#reranker-model').value = cfg.reranker_model ?? '';
  $('#kiwix-exe').value = cfg.kiwix_exe ?? '';
  $('#llama-exe').value = cfg.llama_exe ?? '';
  $('#autostart').checked = cfg.autostart;

  const st = await invoke('get_status');
  const note = $('#portable-note');
  if (st.portable_root) {
    note.textContent = `Running from ${st.portable_root}. Paths are kept relative to that folder, so the drive works under any letter; the model, helper build and GPU split chosen for this computer are remembered on this computer, not on the drive.`;
    note.hidden = false;
  } else {
    note.hidden = true;
  }
  const sp = await invoke('library_space').catch(() => null);
  const spaceLine = sp ? ` ${gb(sp.library_bytes)} on disk, ${gb(sp.free_bytes)} free on ${sp.volume}.` : '';
  $('#library-summary').textContent = st.documents
    ? `${fmt(st.documents)} documents, ${fmt(st.pages)} pages, ${fmt(st.words)} words indexed.${spaceLine}`
    : st.library_dir
      ? `Library folder set, but nothing is indexed yet.${spaceLine}`
      : 'No library configured.';
}

$('#pick-library').addEventListener('click', () => pickInto('#library-dir', true, 'Choose your library folder'));
$('#pick-kiwix').addEventListener('click', () => pickInto('#kiwix-exe', false, 'Choose the kiwix-serve program', 'program'));
$('#pick-llama').addEventListener('click', () => pickInto('#llama-exe', false, 'Choose the llama-server program', 'program'));
$('#pick-chat').addEventListener('click', () => pickInto('#chat-model', false, 'Choose a chat model', 'gguf'));
$('#pick-reranker').addEventListener('click', () => pickInto('#reranker-model', false, 'Choose a reranker model', 'gguf'));

$('#save-config').addEventListener('click', async () => {
  const nz = (v) => (v.trim() ? v.trim() : null);
  const current = await invoke('get_config');
  const config = {
    ...current,
    library_dir: nz($('#library-dir').value),
    kiwix_exe: nz($('#kiwix-exe').value),
    llama_exe: nz($('#llama-exe').value),
    chat_model: nz($('#chat-model').value),
    reranker_model: nz($('#reranker-model').value),
  };
  try {
    await invoke('set_config', { config });
    statusMsg('Saved.');
    await refreshSetup();
  } catch (e) {
    statusMsg(String(e), true);
  }
});

$('#start-services').addEventListener('click', async () => {
  statusMsg('Starting…');
  setDot('chat', 'busy');
  try {
    await invoke('start_services');
    statusMsg('Services running.');
  } catch (e) {
    statusMsg(String(e), true);
  }
  await refreshServiceDots();
});

$('#stop-services').addEventListener('click', async () => {
  // An unhandled rejection here once left the button looking dead with no
  // message while the model stayed resident on the GPU. Errors must reach
  // the person.
  try {
    await invoke('stop_services');
    statusMsg('Stopped. The GPU is free again.');
  } catch (e) {
    statusMsg(`Could not stop services: ${String(e)}`, true);
  }
  await refreshServiceDots();
});

listen('service-progress', (e) => statusMsg(e.payload));
listen('index-progress', (e) => statusMsg(e.payload));

/* ----------------------------------------------------------- extra folders */

async function refreshExtraDirs() {
  const box = $('#extra-dirs');
  const cfg = await invoke('get_config').catch(() => null);
  box.textContent = '';
  const dirs = cfg?.extra_dirs ?? [];
  if (!dirs.length) {
    box.append(el('p', 'hint', 'No extra folders yet.'));
    return;
  }
  for (const d of dirs) {
    const row = el('div', 'item');
    row.append(el('span', 'path', d));
    const rm = el('button', undefined, 'Remove');
    rm.type = 'button';
    rm.addEventListener('click', async () => {
      rm.disabled = true;
      try {
        statusMsg(await invoke('remove_content_dir', { path: d }));
      } catch (e) {
        statusMsg(String(e), true);
      }
      await refreshExtraDirs();
      await refreshSetup();
    });
    row.append(rm);
    box.append(row);
  }
}

$('#add-dir').addEventListener('click', async () => {
  const chosen = await chooseFile({ title: 'Add a folder of content', want: 'dir' });
  if (!chosen) return;
  const btn = $('#add-dir');
  btn.setAttribute('disabled', 'true');
  statusMsg(`Adding ${chosen}…`);
  try {
    statusMsg(await invoke('add_content_dir', { path: chosen }));
  } catch (e) {
    statusMsg(String(e), true);
  } finally {
    btn.removeAttribute('disabled');
    await refreshExtraDirs();
    await refreshSetup();
  }
});

/* ------------------------------------------------------------- maintenance */

$('#build-index').addEventListener('click', async () => {
  const btn = $('#build-index');
  btn.setAttribute('disabled', 'true');
  statusMsg('Building the index…');
  try {
    statusMsg(await invoke('build_index', { force: false }));
    await refreshSetup();
  } catch (e) {
    statusMsg(String(e), true);
  } finally {
    btn.removeAttribute('disabled');
  }
});

$('#fit-model').addEventListener('click', async () => {
  const btn = $('#fit-model');
  btn.setAttribute('disabled', 'true');
  statusMsg('Measuring the model against this GPU — about ten seconds…');
  try {
    statusMsg(`Fitted: ${await invoke('fit_chat_model')} Saved.`);
  } catch (e) {
    statusMsg(String(e), true);
  } finally {
    btn.removeAttribute('disabled');
  }
});

/* ---------------------------------------------------------------- advanced */

async function refreshTuning() {
  try {
    const info = await invoke('tuning_info');
    $('#tuning-path').textContent = info.exists
      ? `${info.path} — in use, read before every question.`
      : `${info.path} — not created yet; the built-in defaults are in use.`;
    $('#tuning-open').textContent = info.exists ? 'Open the override file' : 'Create and open the override file';
  } catch (e) {
    $('#tuning-path').textContent = String(e);
  }
}

$('#tuning-open').addEventListener('click', async () => {
  try {
    const path = await invoke('write_default_tuning');
    await invoke('open_path', { path });
  } catch (e) {
    statusMsg(String(e), true);
  } finally {
    // Created even when it could not be opened here; say so.
    await refreshTuning();
  }
});

$('#rerun-setup').addEventListener('click', (e) => {
  e.preventDefault();
  showWelcome();
});

$('#autostart').addEventListener('change', async () => {
  const current = await invoke('get_config');
  await invoke('set_config', { config: { ...current, autostart: $('#autostart').checked } }).catch((e) =>
    statusMsg(String(e), true),
  );
});

onTab('setup', refreshSetup);
onTab('setup', refreshExtraDirs);
onTab('setup', refreshTuning);
