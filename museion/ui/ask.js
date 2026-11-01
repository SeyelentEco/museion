// The Ask tab: the question, the answer as it streams, where it came from,
// the calculations, and the sources.

import { invoke, listen } from './api.js';
import { renderAnswer } from './answer.js';
import { $, el } from './dom.js';
import { openSource } from './library.js';
import { showTab } from './tabs.js';

let busy = false;

// The conversation so far, as the person saw it, so a follow-up ("what
// about the placenta?", "this baby") is read against what came before. It
// lives for the page; there is no memory across launches, by design.
const conversation = [];

// Where an answer came from, judged by the tools that ran -- not by anything
// the model claimed. The unverified case is the one that must never pass as
// library material.
const BASIS_LABELS = {
  library: ['From your library', ''],
  library_and_calculation: ['From your library, with calculation', ''],
  computed: ['Computed', ''],
  general_knowledge: ['General knowledge — not from your library. Unverified.', 'warn'],
};

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

async function askQuestion(question) {
  if (busy || !question.trim()) return;
  busy = true;
  $('#ask-send').setAttribute('disabled', 'true');
  $('#ask-empty')?.remove();

  const transcript = $('#transcript');
  const exchange = el('div', 'exchange');
  exchange.append(el('div', 'question', question));
  transcript.append(exchange);
  transcript.scrollTop = transcript.scrollHeight;

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
    busy = false;
    $('#ask-send').removeAttribute('disabled');
    transcript.scrollTop = transcript.scrollHeight;
    return;
  }

  const answerEl = el('div', 'answer thinking', 'Searching the library…');
  exchange.append(answerEl);
  transcript.scrollTop = transcript.scrollHeight;

  let raw = '';
  let started = false;
  // If nothing has arrived after a while, say what is being waited on. The
  // reranker and the model rewrite can take ten seconds; silence reads as a hang.
  const slow = setTimeout(() => {
    if (!started) answerEl.textContent = 'Still working — retrieving passages and waiting on the model…';
  }, 12000);
  const unlisten = listen('answer-token', (e) => {
    if (!started) {
      started = true;
      clearTimeout(slow);
      answerEl.classList.remove('thinking');
      answerEl.textContent = '';
    }
    raw += e.payload;
    renderAnswer(answerEl, raw);
    transcript.scrollTop = transcript.scrollHeight;
  });
  // The model narrated its way to a tool call; that narration is not the
  // answer and may contain numbers it guessed. Clear it and say what is
  // happening, so a person sees "checking" rather than a wrong figure.
  const unlistenReset = listen('answer-reset', () => {
    raw = '';
    started = false;
    answerEl.className = 'answer thinking';
    answerEl.textContent = 'Checking the library and working it out…';
  });

  try {
    const { text, sources, basis, calculations } = await invoke('ask', {
      question,
      history: conversation.slice(-4),
    });
    clearTimeout(slow);
    // The final text, not what streamed: its citations are numbered to match
    // the sources below, and the last tokens of the stream may still be on
    // their way when the result arrives.
    conversation.push({ question, answer: text });
    renderAnswer(answerEl, text);
    answerEl.classList.remove('thinking');

    // The calculator's actual outputs, verbatim. If the prose above says
    // something different, the prose is wrong -- and now that is visible.
    if (calculations.length) {
      const box = el('div', 'calcs');
      box.append(el('h3', undefined, 'Calculations performed'));
      for (const c of calculations) {
        const row = el('div', 'calc');
        row.append(el('code', undefined, c.expression));
        row.append(el('span', 'eq', ' = '));
        row.append(el('strong', undefined, c.result));
        box.append(row);
      }
      exchange.append(box);
    }

    const [labelText, cls] = BASIS_LABELS[basis] ?? ['', ''];
    if (labelText) exchange.insertBefore(el('div', `basis ${cls}`.trim(), labelText), answerEl);

    if (sources.length) exchange.append(sourceList(sources));
  } catch (err) {
    clearTimeout(slow);
    const msg = String(err);
    // Service problems get the notice with the link; anything else is an error.
    if (/isn't running|still loading|not set up/i.test(msg)) {
      answerEl.remove();
      serviceNotice(exchange, msg);
    } else {
      answerEl.className = 'error';
      answerEl.textContent = msg;
    }
  } finally {
    unlisten();
    unlistenReset();
    busy = false;
    $('#ask-send').removeAttribute('disabled');
    transcript.scrollTop = transcript.scrollHeight;
  }
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

$('#ask-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('#question');
  const q = input.value;
  input.value = '';
  void askQuestion(q);
});

document.querySelectorAll('#examples li').forEach((li) =>
  li.addEventListener('click', () => void askQuestion(li.textContent.trim())),
);
