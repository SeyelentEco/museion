// The list of chats beside the Ask tab: newest first, each with its agent,
// filtered by agent if wanted, and a New chat button with the agent to use.

import { invoke } from './api.js';
import { $, el } from './dom.js';
import { currentChat, openChat } from './ask.js';
import { onTab } from './tabs.js';

let agentsKnown = [];

export function selectedAgent() {
  return $('#new-chat-agent').value || 'librarian';
}

/** Start a chat with an agent and show it (the Agents tab calls this too). */
export async function startChat(agent) {
  try {
    const chat = await invoke('new_chat', { agent });
    await refreshChats();
    await openChat(chat.id);
    $('#question').focus();
  } catch (err) {
    alert(String(err));
  }
}

export async function refreshChats() {
  let data;
  try {
    data = await invoke('list_chats', { agent: $('#chat-filter').value || null });
  } catch {
    return; // not set up yet: the Ask tab still shows its empty page
  }
  if (JSON.stringify(data.agents) !== JSON.stringify(agentsKnown)) {
    agentsKnown = data.agents;
    for (const sel of [$('#new-chat-agent'), $('#chat-filter')]) {
      const keep = sel.value;
      sel.querySelectorAll('option[data-agent]').forEach((o) => o.remove());
      for (const a of data.agents) {
        if (sel.id === 'new-chat-agent' && !a.can_start) continue;
        const o = el('option', undefined, a.name);
        o.value = a.id;
        o.dataset.agent = '1';
        sel.append(o);
      }
      sel.value = keep;
    }
    if (!$('#new-chat-agent').value) $('#new-chat-agent').value = 'librarian';
  }
  const list = $('#chat-items');
  list.textContent = '';
  if (!data.chats.length) list.append(el('li', 'empty-note', 'No chats yet. Ask something to start one.'));
  for (const c of data.chats) {
    const li = el('li');
    li.dataset.id = c.id;
    li.classList.toggle('active', currentChat()?.id === c.id);
    li.append(el('div', 't', c.title));
    const meta = el('div', 'meta', c.agent_name);
    if (c.working) meta.append(el('span', 'badge', 'working'));
    li.append(meta);
    li.addEventListener('click', () => void openChat(c.id));
    list.append(li);
  }
}

$('#new-chat').addEventListener('click', () => void startChat(selectedAgent()));
$('#chat-filter').addEventListener('change', () => void refreshChats());

$('#chat-rename').addEventListener('click', async () => {
  const chat = currentChat();
  if (!chat) return;
  const title = prompt('Name this chat:', chat.title || '');
  if (title === null) return;
  await invoke('rename_chat', { id: chat.id, title }).catch((e) => alert(String(e)));
  await refreshChats();
  await openChat(chat.id);
});

$('#chat-delete').addEventListener('click', async () => {
  const chat = currentChat();
  if (!chat || !confirm(`Delete the chat "${chat.title || 'New chat'}"? This cannot be undone.`)) return;
  try {
    await invoke('delete_chat', { id: chat.id });
  } catch (e) {
    alert(String(e));
    return;
  }
  await openChat(null);
  await refreshChats();
});

onTab('ask', refreshChats);
