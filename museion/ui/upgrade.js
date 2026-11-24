// Settings › Version and upgrades (museion/upgrade_commands.py).
//
// Upgrading this copy ends with Museion itself being replaced, so the page
// outlives the program for a moment: it asks for the upgrade to be prepared,
// tells Museion to quit, waits for the new one to answer, and reloads.

import { closeEvents, invoke, listen } from './api.js';
import { chooseFile, pickInto } from './chooser.js';
import { $, el } from './dom.js';
import { onTab, showTab } from './tabs.js';

const UPGRADED = 'museion-upgraded';
const status = (text, err = false) => {
  const out = $('#upgrade-status');
  out.textContent = text;
  out.classList.toggle('err', err);
};
const buttons = () => ['#upgrade-start', '#upgrade-back', '#upgrade-other'].map((id) => $(id));
const busy = (on) => buttons().forEach((b) => (on ? b.setAttribute('disabled', 'true') : b.removeAttribute('disabled')));
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/** What the last upgrade did, in the words of the command line's summary. */
function describe(last) {
  if (!last) return '';
  if (last.back) return `Went back from version ${last.from} to ${last.to}.`;
  const lines = [`Upgraded ${last.from ? `from version ${last.from} ` : ''}to ${last.to}.`];
  if (last.carried?.length) {
    lines.push(`${plural(last.carried.length, 'change', 'changes')} you had made to the code ${last.carried.length === 1 ? 'was' : 'were'} carried over.`);
  }
  if (last.aside?.length) {
    lines.push(last.dropped_changes
      ? 'The new version did not pass its tests with your changes in it, so they were set aside:'
      : `${plural(last.aside.length, 'change', 'changes')} could not be carried over:`);
    lines.push(`  ${last.aside.join(', ')}`);
    lines.push(`Nothing is lost. Your files are in ${last.aside_folder}, and app/UPGRADE-NOTES.md says how to put each change back; Edit Museion can do it.`);
  }
  return lines.join('\n');
}

export async function refreshUpgrade() {
  let st;
  try {
    st = await invoke('upgrade_state');
  } catch (e) {
    $('#upgrade-summary').textContent = String(e);
    return;
  }
  $('#upgrade-summary').textContent = st.root
    ? `This is Museion ${st.version}, in ${st.root}.`
    : `This is Museion ${st.version}, running from a git checkout. Bring it up to date with git pull.`;
  $('#upgrade-own').hidden = !st.root;

  const last = $('#upgrade-last');
  last.textContent = describe(st.last);
  last.hidden = !st.last;

  const back = $('#upgrade-back');
  back.hidden = !st.previous;
  back.textContent = `Go back to version ${st.previous}`;

  const changes = Object.entries(st.changes ?? {});
  const box = $('#upgrade-changes');
  box.hidden = changes.length === 0;
  box.querySelector('summary').textContent = `You have changed ${plural(changes.length, 'file', 'files')} of Museion's code`;
  const list = box.querySelector('ul');
  list.textContent = '';
  for (const [path, kind] of changes) {
    const li = el('li', undefined, path);
    li.append(el('span', undefined, ` (${kind})`));
    list.append(li);
  }
  if (st.failed) status(`Museion was not changed: ${st.failed}`, true);
}

/** Quit Museion and reload this page once the one that replaces it answers. */
async function restartInto(what) {
  closeEvents();
  await fetch('/api/quit', { method: 'POST', headers: { 'X-Museion': '1' } }).catch(() => {});
  try {
    sessionStorage.setItem(UPGRADED, '1');
  } catch { /* private window: the page just opens on Ask */ }
  let wentDown = false;
  for (let tries = 0; tries < 600; tries += 1) {
    await new Promise((r) => setTimeout(r, 1000));
    const up = await fetch('/api/hello').then((r) => r.ok).catch(() => false);
    if (!up) wentDown = true;
    else if (wentDown) return location.reload();
    status(wentDown ? `${what} Museion is restarting; this page reloads by itself when it is back.` : `${what} Waiting for Museion to close…`);
  }
  status('Museion did not come back. Start it again from its folder.', true);
}

$('#upgrade-pick').addEventListener('click', async () => {
  const chosen = await chooseFile({ title: 'Choose the newer Museion download', want: 'package', start: $('#upgrade-source').value });
  if (chosen) $('#upgrade-source').value = chosen;
});
$('#upgrade-target-pick').addEventListener('click', () => pickInto('#upgrade-target', true, "Choose the other copy's folder"));

$('#upgrade-start').addEventListener('click', async () => {
  const path = $('#upgrade-source').value.trim();
  if (!path) return status('Choose the newer download, or type the folder of a newer copy.', true);
  busy(true);
  status('Preparing the upgrade…');
  const off = listen('upgrade-progress', (e) => status(e.payload.line));
  try {
    const r = await invoke('upgrade_from', { path, port: Number(location.port) });
    off();
    await restartInto(`Version ${r.to} is ready.`);
  } catch (e) {
    off();
    status(String(e), true);
    busy(false);
  }
});

$('#upgrade-back').addEventListener('click', async () => {
  if (!confirm(`${$('#upgrade-back').textContent}? Museion restarts. The version you have now is kept, so this can be undone the same way.`)) return;
  busy(true);
  try {
    const r = await invoke('upgrade_back', { port: Number(location.port) });
    await restartInto(`Going back to version ${r.to}.`);
  } catch (e) {
    status(String(e), true);
    busy(false);
  }
});

$('#upgrade-other').addEventListener('click', async () => {
  const path = $('#upgrade-target').value.trim();
  if (!path) return status("Choose the other copy's folder.", true);
  busy(true);
  status('Looking at that copy…');
  const off = listen('upgrade-progress', (e) => status(e.payload.line));
  try {
    status(await invoke('upgrade_other', { path }));
  } catch (e) {
    status(String(e), true);
  } finally {
    off();
    busy(false);
  }
});

onTab('setup', refreshUpgrade);

// Back from an upgrade: show what it did, not the Ask tab.
try {
  if (sessionStorage.getItem(UPGRADED)) {
    sessionStorage.removeItem(UPGRADED);
    setTimeout(() => {
      if ($('header nav').hasAttribute('hidden')) return; // first-run set-up is showing
      showTab('setup');
      $('#card-upgrade').scrollIntoView({ block: 'start' });
    }, 1200);
  }
} catch { /* no session storage */ }
