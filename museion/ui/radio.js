// The Radio tab and the Radio card in Settings: short text messages by a
// Meshtastic radio, an optional accessory (museion/radio.py). The tab only
// appears once a radio has been connected here; without one, nothing on
// the page changes.

import { invoke, listen } from './api.js';
import { $, el } from './dom.js';
import { onTab, showTab } from './tabs.js';

// As in radio.py: what one radio message carries, and how many parts a
// long one may be cut into.
const MAX_BYTES = 200;
const MAX_PARTS = 4;

let state = { address: '', connected: false, error: '', me: '', nodes: [] };
let loaded = false;
let unread = 0;
let sending = false;

const bytes = (text) => new TextEncoder().encode(text).length;
const radioTabOpen = () => $('#panel-radio').classList.contains('active');

// ── the state: shown on the tab, its side panel and the Settings card ──

function showState(s) {
  state = s;
  const tab = $('#tab-radio');
  tab.hidden = !s.address;
  tab.textContent = unread ? `Radio (${unread})` : 'Radio';

  const line = $('#radio-status');
  if (s.connected) {
    line.textContent = `Connected${s.me ? ` as ${s.me}` : ''}, on ${s.address}.`;
  } else if (s.address) {
    line.textContent = `Not connected to ${s.address}${s.error ? `: ${s.error}` : ''}. Museion tries again every few seconds.`;
  } else {
    line.textContent = 'No radio is connected. Connect one in Settings.';
  }
  line.className = `small-print ${s.connected ? 'good' : s.address ? 'err' : 'muted'}`;

  const list = $('#radio-nodes');
  list.textContent = '';
  if (!s.nodes.length) list.append(el('li', 'empty-note', s.connected ? 'None yet.' : 'None while it is not connected.'));
  for (const n of s.nodes) {
    const li = el('li');
    li.append(el('div', 't', n));
    li.title = 'Send to this radio only';
    li.addEventListener('click', () => {
      $('#radio-to').value = n;
      $('#radio-text').focus();
    });
    list.append(li);
  }

  const to = $('#radio-to');
  const chosen = to.value;
  to.textContent = '';
  const all = el('option', undefined, 'Everyone in range');
  all.value = '';
  to.append(all);
  for (const n of s.nodes) {
    const o = el('option', undefined, n);
    o.value = n;
    to.append(o);
  }
  to.value = s.nodes.includes(chosen) ? chosen : '';
  $('#radio-send-button').disabled = !s.connected;

  // The Settings card.
  const card = $('#radio-card-status');
  card.textContent = s.connected
    ? `Connected${s.me ? ` as ${s.me}` : ''}, on ${s.address}. The Radio tab shows the messages.`
    : s.address
      ? `Connecting to ${s.address}…${s.error ? ` Last try: ${s.error}` : ''}`
      : '';
  card.className = s.address && !s.connected && s.error ? 'status err' : 'status';
  $('#radio-disconnect').hidden = !s.address;
}

async function refreshState() {
  try {
    showState(await invoke('radio_state'));
  } catch {
    // Without the state there is nothing to show: the tab stays hidden.
  }
}

// ── the messages ──

function messageItem(m) {
  const li = el('li', `radio-msg ${m.direction === 'out' ? 'out' : 'in'}`);
  const who = m.direction === 'out' ? `you → ${m.to}` : `${m.from}${m.to && m.to !== 'everyone' ? ` → ${m.to}` : ''}`;
  const channel = Number(m.channel) ? ` · channel ${m.channel}` : '';
  li.append(el('div', 'meta', `${m.time} · ${who}${channel}`));
  li.append(el('div', 't', m.text));
  return li;
}

function append(m) {
  const log = $('#radio-log');
  log.querySelector('.empty-note')?.remove();
  log.append(messageItem(m));
  log.scrollTop = log.scrollHeight;
}

async function loadMessages() {
  unread = 0;
  showState(state);
  const log = $('#radio-log');
  let messages;
  try {
    messages = await invoke('radio_messages', { limit: 200 });
  } catch (e) {
    log.textContent = '';
    log.append(el('li', 'empty-note', String(e)));
    return;
  }
  log.textContent = '';
  if (!messages.length) log.append(el('li', 'empty-note', 'No messages yet. What comes in by radio shows here, and is kept in your notes.'));
  for (const m of messages) log.append(messageItem(m));
  log.scrollTop = log.scrollHeight;
  loaded = true;
  if (state.connected) $('#radio-text').focus();
}

function countParts() {
  const n = bytes($('#radio-text').value.trim());
  const count = $('#radio-count');
  const parts = n <= MAX_BYTES ? 1 : Math.ceil(n / (MAX_BYTES - 8));
  count.className = parts > MAX_PARTS ? 'err' : '';
  count.textContent = parts > MAX_PARTS ? 'Too long to send by radio.' : parts > 1 ? `Goes as ${parts} messages.` : '';
}

// ── the Settings card ──

async function find() {
  const button = $('#radio-find');
  const out = $('#radio-found');
  button.disabled = true;
  button.textContent = 'Looking…';
  out.textContent = '';
  try {
    const found = await invoke('find_radios');
    if (!found.length) {
      out.append(el('span', 'muted', 'No radio answered on this computer’s ports. Check it is plugged in and switched on, and that no other program has it open; one on the network is tcp: and its address.'));
    }
    for (const r of found) {
      const b = el('button', 'small', `${r.address}${r.name ? ` — ${r.name}` : ''}`);
      b.type = 'button';
      b.addEventListener('click', () => ($('#radio-address').value = r.address));
      out.append(b);
    }
    if (found.length === 1 && !$('#radio-address').value.trim()) $('#radio-address').value = found[0].address;
  } catch (e) {
    out.append(el('span', 'muted', String(e)));
  } finally {
    button.disabled = false;
    button.textContent = 'Look for a radio';
  }
}

async function connect() {
  const status = $('#radio-card-status');
  try {
    showState(await invoke('connect_radio', { address: $('#radio-address').value }));
  } catch (e) {
    status.textContent = String(e);
    status.className = 'status err';
  }
}

async function disconnect() {
  try {
    showState(await invoke('disconnect_radio'));
    $('#radio-found').textContent = '';
    $('#radio-card-status').textContent = 'Disconnected. The messages so far stay in your notes.';
  } catch (e) {
    $('#radio-card-status').textContent = String(e);
  }
}

async function refreshCard() {
  await refreshState();
  if (!$('#radio-address').value) $('#radio-address').value = state.address;
}

// ── wiring ──

$('#radio-send').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#radio-text');
  const text = input.value.trim();
  // The button stays enabled while a long message goes out part by part
  // (Enter does nothing in a form whose button is disabled), so a second
  // press is ignored here instead.
  if (!text || sending) return;
  sending = true;
  const button = $('#radio-send-button');
  button.textContent = 'Sending…';
  try {
    // What is sent comes back as a radio-message event, like what comes in.
    await invoke('send_radio_message', { text, to: $('#radio-to').value });
    if (input.value.trim() === text) input.value = '';
    countParts();
  } catch (err) {
    $('#radio-count').className = 'err';
    $('#radio-count').textContent = `${String(err)} `;
  } finally {
    sending = false;
    button.textContent = 'Send';
    input.focus();
  }
});
$('#radio-text').addEventListener('input', countParts);
$('#radio-settings').addEventListener('click', () => {
  showTab('setup');
  $('#card-radio').scrollIntoView({ block: 'start' });
});
$('#radio-find').addEventListener('click', () => void find());
$('#radio-connect').addEventListener('click', () => void connect());
$('#radio-disconnect').addEventListener('click', () => void disconnect());
$('#radio-address').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') void connect();
});

listen('radio-state', (e) => showState(e.payload));
listen('radio-message', (e) => {
  if (loaded) append(e.payload);
  if (!radioTabOpen() && e.payload.direction === 'in') {
    unread += 1;
    showState(state);
  }
});

onTab('radio', loadMessages);
onTab('setup', refreshCard);
void refreshState();
