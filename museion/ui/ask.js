// The Ask tab: chats, each with one agent. The question, the answer as it
// streams, what the agent did on the way, where the answer came from, the
// calculations and the sources. The list of chats is chats.js.

import { invoke, listen } from './api.js';
import { renderAnswer } from './answer.js';
import { $, el } from './dom.js';
import { openSource } from './library.js';
import { showTab } from './tabs.js';
import { refreshChats, selectedAgent } from './chats.js';

// The chat on screen: { id, agent, agent_info, messages, working, waiting }.
let current = null;
// The exchange being worked on in the chat on screen, while its agent runs.
let live = null;

// Where an answer came from, judged by the tools that ran -- not by anything
// the model claimed. The unverified case is the one that must never pass as
// library material.
const BASIS_LABELS = {
  library: ['From your library', ''],
  library_and_calculation: ['From your library, with calculation', ''],
  computed: ['Computed', ''],
  own_files: ['From the files it checked', ''],
  general_knowledge: ['General knowledge — not from your library. Unverified.', 'warn'],
};

// What each tool call looks like in the list of steps.
function stepText(s) {
  const a = s.args ?? {};
  switch (s.kind) {
    case 'tool':
      if (s.refused) return `Tried ${s.tool}, but the call was malformed`;
      return {
        search_library: `Searched the library for “${a.query ?? ''}”`,
        calculate: `Calculated ${a.expression ?? ''}`,
        read_page: `Read passage [${a.number}] in full`,
        write_file: `Wrote ${a.name ?? ''}`,
        read_file: `Read ${a.name ?? ''}`,
        list_files: 'Looked at its files',
        notes: a.action === 'read' ? 'Read its notes' : 'Updated its notes',
        ask_person: `Asked: ${a.question ?? ''}`,
        delegate: `Asked ${a.agent ?? 'another agent'}: ${a.task ?? ''}`,
        library_files: 'Listed the library folder',
        add_source: `Added ${a.file ?? ''} to the catalogue`,
      }[s.tool] ?? `Used ${s.tool}`;
    case 'approval':
      return s.decision === 'deny' ? `You declined ${s.tool}` : `You allowed ${s.tool}`;
    case 'guard':
      return 'Sent its answer back to be checked';
    case 'compact':
      return 'Folded away old search results to make room';
    case 'delegate':
      return `Handed a task to ${s.agent}`;
    default:
      return s.kind;
  }
}

function stepsBox(steps) {
  const box = el('details', 'steps');
  const sum = el('summary', undefined, '');
  box.append(sum);
  const list = el('ol');
  box.append(list);
  const count = () => {
    const n = list.children.length;
    sum.textContent = n ? `${n} step${n === 1 ? '' : 's'}` : 'Working…';
    box.hidden = n === 0;
  };
  box.add = (s) => {
    const li = el('li', s.depth ? 'sub' : undefined, (s.depth ? '↳ ' : '') + stepText(s));
    list.append(li);
    count();
  };
  for (const s of steps ?? []) box.add(s);
  count();
  return box;
}

/** A notice in the transcript with a link to Settings. */
function serviceNotice(exchange, text) {
  const n = el('div', 'notice');
  n.append(document.createTextNode(text + ' '));
  const link = el('a', 'goto', 'Open Settings');
  link.setAttribute('href', '#');
  link.addEventListener('click', (e) => {
    e.preventDefault();
    showTab('setup');
  });
  n.append(link);
  exchange.append(n);
}

/** One finished answer, as saved in the chat. */
function renderReply(exchange, reply) {
  if (reply.steps?.length) exchange.append(stepsBox(reply.steps));
  const [labelText, cls] = BASIS_LABELS[reply.basis] ?? ['', ''];
  if (labelText) exchange.append(el('div', `basis ${cls}`.trim(), labelText));
  const answerEl = el('div', reply.stopped ? 'answer muted' : 'answer');
  renderAnswer(answerEl, reply.text);
  exchange.append(answerEl);
  // The calculator's actual outputs, verbatim. If the prose above says
  // something different, the prose is wrong -- and now that is visible.
  if (reply.calculations?.length) {
    const box = el('div', 'calcs');
    box.append(el('h3', undefined, 'Calculations performed'));
    for (const c of reply.calculations) {
      const row = el('div', 'calc');
      row.append(el('code', undefined, c.expression));
      row.append(el('span', 'eq', ' = '));
      row.append(el('strong', undefined, c.result));
      box.append(row);
    }
    exchange.append(box);
  }
  if (reply.files?.length) exchange.append(el('div', 'files', `Saved in the agent's folder: ${reply.files.join(', ')}`));
  if (reply.sources?.length) exchange.append(sourceList(reply.sources));
}

function sourceList(sources) {
  const box = el('div', 'sources');
  box.append(el('h3', undefined, 'Sources'));
  for (const s of sources) {
    const row = el('div', 'source');
    row.setAttribute('data-n', String(s.n));
    row.title = s.page > 0 ? `Open the PDF at page ${s.page}` : 'Open the article';
    row.addEventListener('click', () => void openSource(s));
    row.append(el('span', 'n', String(s.n)));
    const body = el('div');
    const t = el('div', 't', s.title);
    t.append(el('span', 'open', '↗'));
    body.append(t);
    // Articles from Wikipedia-style archives have no page; name the archive.
    const meta = [s.author, s.page > 0 ? `page ${s.page}` : s.book].filter(Boolean).join(' — ');
    body.append(el('div', 'meta', meta));
    row.append(body);
    box.append(row);
  }
  return box;
}

// ── the chat on screen ───────────────────────────────────────────────────────

function showEmpty(info) {
  const transcript = $('#transcript');
  transcript.querySelectorAll('.exchange').forEach((x) => x.remove());
  const empty = $('#ask-empty');
  empty.hidden = false;
  if (info && info.id !== 'librarian') {
    $('#empty-title').textContent = info.name;
    $('#empty-lede').textContent = info.goal;
  } else {
    $('#empty-title').textContent = 'Ask the library';
    $('#empty-lede').textContent =
      "Answers come only from the archives on this disk, with citations you can open and read yourself. If the library doesn't cover something, it will say so.";
  }
  if (info?.examples?.length) {
    const list = $('#examples');
    list.textContent = '';
    for (const ex of info.examples) {
      const li = el('li', undefined, ex);
      li.addEventListener('click', () => void send(ex));
      list.append(li);
    }
  }
}

export async function openChat(id) {
  if (!id) {
    current = null;
    live = null;
    $('#chat-head').hidden = true;
    setWorking(false);
    showEmpty(null);
    return;
  }
  const chat = await invoke('get_chat', { id });
  current = chat;
  live = null;
  $('#chat-head').hidden = false;
  $('#chat-agent').textContent = chat.agent_info.name;
  $('#chat-title').textContent = chat.title || 'New chat';
  $('#question').placeholder = chat.agent === 'librarian' ? 'Ask a question…' : `Message the ${chat.agent_info.name.toLowerCase()}…`;
  showEmpty(chat.agent_info);
  const transcript = $('#transcript');
  const msgs = chat.messages;
  if (msgs.length) $('#ask-empty').hidden = true;
  for (let i = 0; i < msgs.length; i++) {
    if (msgs[i].role !== 'user') continue;
    const exchange = el('div', 'exchange');
    exchange.append(el('div', 'question', msgs[i].text));
    const reply = msgs[i + 1]?.role === 'assistant' ? msgs[i + 1] : null;
    if (reply) renderReply(exchange, reply);
    transcript.append(exchange);
    if (!reply && chat.working) live = startLive(exchange);
  }
  if (chat.working && chat.waiting) showCard(chat.waiting);
  setWorking(chat.working);
  transcript.scrollTop = transcript.scrollHeight;
  document.querySelectorAll('#chat-items li').forEach((li) => li.classList.toggle('active', li.dataset.id === id));
}

function setWorking(on) {
  $('#ask-send').hidden = on;
  $('#ask-stop').hidden = !on;
  $('#ask-send').toggleAttribute('disabled', on);
}

/** The pending part of an exchange: steps, a card, and the streaming answer. */
function startLive(exchange) {
  const steps = stepsBox([]);
  const card = el('div', 'agent-card');
  card.hidden = true;
  const answerEl = el('div', 'answer thinking', 'Working…');
  exchange.append(steps, card, answerEl);
  return { exchange, steps, card, answerEl, raw: '', started: false };
}

function showCard(w) {
  if (!live) return;
  const card = live.card;
  card.textContent = '';
  card.hidden = !w?.kind;
  if (!w?.kind) return;
  if (w.kind === 'approve') {
    card.append(el('div', 'card-title', `${w.agent} wants to ${w.summary}`));
    if (w.reason) card.append(el('div', 'muted', w.reason));
    const note = el('input');
    note.type = 'text';
    note.placeholder = 'Why not? (optional, told to the agent)';
    const row = el('div', 'row');
    const allow = el('button', 'primary', 'Allow');
    const always = el('button', undefined, 'Allow for this run');
    const deny = el('button', undefined, 'Deny');
    const reply = (decision) => {
      card.hidden = true;
      void invoke('answer_agent', { chat: current.id, decision, note: note.value }).catch((e) => alert(String(e)));
    };
    allow.addEventListener('click', () => reply('allow'));
    always.addEventListener('click', () => reply('allow_run'));
    deny.addEventListener('click', () => reply('deny'));
    row.append(allow, always, deny);
    card.append(row, note);
  } else if (w.kind === 'ask') {
    card.append(el('div', 'card-title', w.question));
    const form = el('form', 'row');
    const input = el('input');
    input.type = 'text';
    input.placeholder = 'Your answer';
    const go = el('button', 'primary', 'Answer');
    go.type = 'submit';
    form.append(input, go);
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      card.hidden = true;
      void invoke('answer_agent', { chat: current.id, answer: input.value }).catch((err) => alert(String(err)));
    });
    card.append(form);
    setTimeout(() => input.focus(), 0);
  }
  $('#transcript').scrollTop = $('#transcript').scrollHeight;
}

const mine = (e) => current && e.payload.chat === current.id && live;

listen('agent-token', (e) => {
  if (!mine(e)) return;
  if (!live.started) {
    live.started = true;
    live.answerEl.classList.remove('thinking');
    live.answerEl.textContent = '';
  }
  live.raw += e.payload.text;
  renderAnswer(live.answerEl, live.raw);
  $('#transcript').scrollTop = $('#transcript').scrollHeight;
});
// The model narrated its way to a tool call; that narration is not the
// answer and may contain numbers it guessed. Clear it and say what is
// happening, so a person sees "checking" rather than a wrong figure.
listen('agent-reset', (e) => {
  if (!mine(e)) return;
  live.raw = '';
  live.started = false;
  live.answerEl.className = 'answer thinking';
  live.answerEl.textContent = 'Checking the library and working it out…';
});
listen('agent-step', (e) => {
  if (mine(e)) live.steps.add(e.payload);
});
listen('agent-waiting', (e) => {
  if (mine(e)) showCard(e.payload);
  void refreshChats();
});
listen('agent-queued', (e) => {
  if (mine(e)) live.answerEl.textContent = 'Waiting for another chat to finish…';
});

export async function send(text) {
  if (!text.trim() || (current && current.working)) return;
  if (!current) {
    try {
      const chat = await invoke('new_chat', { agent: selectedAgent() });
      await openChat(chat.id);
    } catch (err) {
      alert(String(err));
      return;
    }
  }
  const chatId = current.id;
  $('#ask-empty').hidden = true;
  const transcript = $('#transcript');
  const exchange = el('div', 'exchange');
  exchange.append(el('div', 'question', text));
  transcript.append(exchange);

  // Check before sending. A question against a model that is not running
  // should get a plain answer immediately, not a spinner.
  const blocker = await invoke('get_status')
    .then((st) =>
      !st.configured
        ? 'Museion is not set up yet.'
        : !st.chat_running
          ? 'The chat model is not running, so there is nothing to answer with.'
          : null,
    )
    .catch(() => null); // let the command report instead
  if (blocker) {
    serviceNotice(exchange, blocker);
    transcript.scrollTop = transcript.scrollHeight;
    return;
  }

  current.working = true;
  live = startLive(exchange);
  live.answerEl.textContent = current.agent === 'librarian' ? 'Searching the library…' : 'Working…';
  setWorking(true);
  transcript.scrollTop = transcript.scrollHeight;
  // If nothing has arrived after a while, say what is being waited on.
  const slow = setTimeout(() => {
    if (live && !live.started && live.exchange === exchange)
      live.answerEl.textContent = 'Still working — retrieving passages and waiting on the model…';
  }, 12000);
  if (!current.title) $('#chat-title').textContent = text.trim().replace(/\s+/g, ' ').slice(0, 80);
  void refreshChats();
  try {
    await invoke('send_message', { chat: chatId, text });
    // The saved answer, not what streamed: its citations are numbered to
    // match the sources, and the last tokens may still be on their way.
    if (current?.id === chatId) await openChat(chatId);
  } catch (err) {
    if (current?.id === chatId) {
      const msg = String(err);
      live?.answerEl.remove();
      if (/isn't running|still loading|not set up/i.test(msg)) serviceNotice(exchange, msg);
      else exchange.append(Object.assign(el('div', 'error'), { textContent: msg }));
      current.working = false;
      live = null;
      setWorking(false);
    }
  } finally {
    clearTimeout(slow);
    void refreshChats();
  }
}

$('#ask-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('#question');
  const q = input.value;
  input.value = '';
  void send(q);
});

$('#ask-stop').addEventListener('click', () => {
  if (current) void invoke('stop_run', { chat: current.id });
});

document.querySelectorAll('#examples li').forEach((li) => li.addEventListener('click', () => void send(li.textContent.trim())));

export function currentChat() {
  return current;
}
