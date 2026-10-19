// The three dots in the header: content server, chat model, reranker.

import { invoke } from './api.js';

const DOT_NAMES = {
  kiwix: 'Content server (kiwix-serve)',
  chat: 'Chat model (llama-server)',
  rerank: 'Reranker (llama-server)',
};

/** state is 'on', 'off' or 'busy'. */
export function setDot(svc, state) {
  const d = document.querySelector(`.dot[data-svc="${svc}"]`);
  if (!d) return;
  d.classList.toggle('on', state === 'on');
  d.classList.toggle('busy', state === 'busy');
  const word = state === 'on' ? 'running' : state === 'busy' ? 'starting' : 'stopped';
  d.title = `${DOT_NAMES[svc] ?? svc}: ${word}`;
}

export async function refreshServiceDots() {
  try {
    // The cheap call: process flags only. Polling the full status opened the
    // index every few seconds and made scrolling stutter.
    const s = await invoke('service_state');
    setDot('kiwix', s.kiwix_running ? 'on' : 'off');
    setDot('chat', s.chat_running ? 'on' : 'off');
    setDot('rerank', s.reranker_running ? 'on' : 'off');
  } catch {
    /* Museion may have stopped; the page says so elsewhere */
  }
}
