import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';

interface Source {
  n: number;
  title: string;
  author: string;
  page: number;
  book: string;
  /** PDF filename for a page source; article path for a native archive. */
  file: string;
  snippet: string;
}

type Basis = 'library' | 'library_and_calculation' | 'computed' | 'general_knowledge';

interface AskResult {
  sources: Source[];
  basis: Basis;
  calculations: { expression: string; result: string }[];
}

interface Status {
  configured: boolean;
  library_dir: string | null;
  documents: number;
  pages: number;
  words: number;
  kiwix_running: boolean;
  chat_running: boolean;
  reranker_running: boolean;
  chat_model: string | null;
  reranker_model: string | null;
}

interface Hardware {
  accelerator: string;
  memory_mb: number;
  system_ram_mb: number;
  tier: string;
  tier_detail: string;
}

interface Config {
  autostart: boolean;
  library_dir: string | null;
  kiwix_exe: string | null;
  llama_exe: string | null;
  pdfium_dir: string | null;
  chat_model: string | null;
  reranker_model: string | null;
  chat_args: string[];
  chat_gpu_layers: number;
  kiwix_port: number;
  chat_port: number;
  reranker_port: number;
}

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;
const el = (tag: string, cls?: string, text?: string) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text) n.textContent = text;
  return n;
};

/* ------------------------------------------------------------------ tabs */

function showTab(name: string) {
  document.querySelectorAll('.tab').forEach((t) =>
    t.setAttribute('aria-current', String((t as HTMLElement).dataset.tab === name)),
  );
  document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
  $(`#panel-${name}`).classList.add('active');
  if (name === 'library') {
    void refreshBrowse();
    void refreshCatalogue();
  }
  if (name === 'setup') {
    void refreshSetup();
    void refreshExtraDirs();
  }
  if (name === 'welcome') void loadPlan();
  if (name === 'evals') void refreshEvals();
}

/**
 * First-run set-up is its own screen, shown when there is nothing else to
 * show. Once the machine is set up it goes away; a link on the Library tab
 * brings it back for a new machine or a fresh start.
 */
function showWelcome() {
  $('header nav').setAttribute('hidden', '');
  showTab('welcome');
}

function leaveWelcome(to: string) {
  $('header nav').removeAttribute('hidden');
  showTab(to);
}

document.querySelectorAll('.tab').forEach((t) =>
  t.addEventListener('click', () => showTab((t as HTMLElement).dataset.tab!)),
);

/* ------------------------------------------------------------------ ask */

let busy = false;

/**
 * Render an answer as light Markdown, building DOM nodes directly.
 *
 * No innerHTML anywhere: the text comes from a language model and is not to
 * be trusted as markup. Only what is recognised becomes structure; everything
 * else stays text. Covers what the model actually emits -- paragraphs,
 * headings, bullet and numbered lists with one level of nesting, bold,
 * italic, inline code -- and turns [1] into a chip that reveals the source it
 * points at, because a citation is only worth having if following it is easy.
 */
function renderAnswer(container: HTMLElement, text: string) {
  container.textContent = '';
  const lines = text.replace(/\r\n?/g, '\n').split('\n');

  let para: string[] = [];
  let list: { el: HTMLElement; ordered: boolean; sub: HTMLElement | null } | null = null;

  const flushPara = () => {
    if (para.length) {
      const p = el('p');
      inline(p, para.join(' '));
      container.append(p);
      para = [];
    }
  };
  const flushList = () => {
    list = null;
  };

  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/, '');
    const indent = rawLine.match(/^\s*/)![0].length;

    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*(?:[-*•])\s+(.*)$/);
    const number = line.match(/^\s*\d+[.)]\s+(.*)$/);

    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    if (heading) {
      flushPara();
      flushList();
      const h = el(heading[1].length <= 2 ? 'h3' : 'h4');
      inline(h, heading[2]);
      container.append(h);
      continue;
    }
    if (bullet || number) {
      flushPara();
      const ordered = Boolean(number);
      const body = (bullet ?? number)![1];
      // An indented item nests under the previous top-level item.
      if (list && indent >= 2 && list.el.lastElementChild) {
        if (!list.sub) {
          list.sub = el(ordered ? 'ol' : 'ul');
          list.el.lastElementChild.append(list.sub);
        }
        const li = el('li');
        inline(li, body);
        list.sub.append(li);
        continue;
      }
      if (!list || list.ordered !== ordered) {
        list = { el: el(ordered ? 'ol' : 'ul'), ordered, sub: null };
        container.append(list.el);
      }
      list.sub = null;
      const li = el('li');
      inline(li, body);
      list.el.append(li);
      continue;
    }
    // A continuation line inside a list item belongs to that item.
    if (list && indent >= 2 && list.el.lastElementChild) {
      const target = (list.sub?.lastElementChild ?? list.el.lastElementChild) as HTMLElement;
      target.append(document.createTextNode(' '));
      inline(target, line.trim());
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushPara();
}

/** Inline markup: **bold**, *italic*, `code`, and [n] citation chips. */
function inline(target: HTMLElement, text: string) {
  const tokens = text.split(/(\*\*[^*]+\*\*|`[^`]+`|(?<![*\w])\*[^*\n]+\*(?!\w)|\[\d+(?:\]\s*,?\s*\[\d+)*\]|\[\d+(?:,\s*\d+)*\])/g);
  for (const t of tokens) {
    if (!t) continue;
    if (t.startsWith('**') && t.endsWith('**')) {
      target.append(el('strong', undefined, t.slice(2, -2)));
    } else if (t.startsWith('`') && t.endsWith('`')) {
      target.append(el('code', undefined, t.slice(1, -1)));
    } else if (t.startsWith('*') && t.endsWith('*') && t.length > 2) {
      target.append(el('em', undefined, t.slice(1, -1)));
    } else if (/^\[[\d,\s\]\[]+\]$/.test(t)) {
      for (const num of t.match(/\d+/g) ?? []) {
        const c = el('span', 'cite', num);
        c.addEventListener('click', () => {
          const row = document.querySelector(`.source[data-n="${num}"]`);
          row?.scrollIntoView({ behavior: 'smooth', block: 'center' });
          row?.classList.add('highlight');
          setTimeout(() => row?.classList.remove('highlight'), 1600);
        });
        target.append(c);
      }
    } else {
      target.append(document.createTextNode(t));
    }
  }
}

/** A notice in the transcript with a link to the Library tab. */
function serviceNotice(exchange: HTMLElement, text: string) {
  const n = el('div', 'notice');
  n.append(document.createTextNode(text + ' '));
  const link = el('a', 'goto', 'Open the Library tab');
  link.setAttribute('href', '#');
  link.addEventListener('click', (e) => {
    e.preventDefault();
    showTab('setup');
  });
  n.append(link);
  exchange.append(n);
}

async function askQuestion(question: string) {
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
  const blocker = await invoke<Status>('get_status')
    .then((st) =>
      !st.configured
        ? 'Museion is not set up yet.'
        : !st.chat_running
          ? 'The chat model is not running, so there is nothing to answer with.'
          : null,
    )
    .catch(() => null); // let the backend report instead
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
  const unlisten = await listen<string>('answer-token', (e) => {
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
  const unlistenReset = await listen('answer-reset', () => {
    raw = '';
    started = false;
    answerEl.className = 'answer thinking';
    answerEl.textContent = 'Checking the library and working it out…';
  });

  try {
    const { sources, basis, calculations } = await invoke<AskResult>('ask', { question });
    renderAnswer(answerEl, raw);
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

    // Say where the answer came from, judged by the tools that ran -- not
    // by anything the model claimed. The unverified case is the one that
    // must never pass as library material.
    const labels: Record<Basis, [string, string]> = {
      library: ['From your library', ''],
      library_and_calculation: ['From your library, with calculation', ''],
      computed: ['Computed', ''],
      general_knowledge: ['General knowledge — not from your library. Unverified.', 'warn'],
    };
    const [labelText, cls] = labels[basis] ?? ['', ''];
    if (labelText) {
      const b = el('div', `basis ${cls}`.trim(), labelText);
      exchange.insertBefore(b, answerEl);
    }

    if (sources.length) {
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
      exchange.append(box);
    }
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

$('#ask-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $<HTMLInputElement>('#question');
  const q = input.value;
  input.value = '';
  void askQuestion(q);
});

document.querySelectorAll('#examples li').forEach((li) =>
  li.addEventListener('click', () => void askQuestion(li.textContent!.trim())),
);

/* ------------------------------------------------------------------ browse */

/**
 * The content server's URL for a source: the article itself, or the PDF
 * opened at the cited page. Citations are only worth having if following one
 * is easy, and "page 129 of Where There Is No Doctor" should be one click.
 */
async function sourceUrl(s: Source): Promise<string> {
  const base = await invoke<string>('kiwix_url');
  const enc = (p: string) => p.split('/').map(encodeURIComponent).join('/');
  return s.page > 0
    ? `${base}/content/${enc(s.book)}/files/${enc(s.file)}#page=${s.page}`
    : `${base}/content/${enc(s.book)}/${enc(s.file)}`;
}

async function openSource(s: Source) {
  // A PDF found in a folder is not served by the content server; hand it to
  // whatever the system opens PDFs with.
  if (s.book.startsWith('folder:')) {
    try {
      await invoke('open_path', { path: s.file });
    } catch (e) {
      statusMsg(String(e), true);
    }
    return;
  }
  const status = await invoke<Status>('get_status');
  if (!status.kiwix_running) {
    statusMsg('The content server is not running; start services to open sources.', true);
    showTab('setup');
    return;
  }
  const frame = $<HTMLIFrameElement>('#browse-frame');
  frame.src = await sourceUrl(s);
  $('#browse-where').textContent = s.page > 0 ? `${s.title} — page ${s.page}` : s.title;
  showTab('library');
}

/** Open an archive's front page in the reader. */
async function openBook(id: string, title: string) {
  const status = await invoke<Status>('get_status');
  if (!status.kiwix_running) {
    statusMsg('The content server is not running; start services to read.', true);
    return;
  }
  const base = await invoke<string>('kiwix_url');
  $<HTMLIFrameElement>('#browse-frame').src = `${base}/content/${encodeURIComponent(id)}/`;
  $('#browse-where').textContent = title;
  document.querySelectorAll('.book').forEach((b) => b.classList.toggle('active', (b as HTMLElement).dataset.id === id));
}

async function refreshBrowse() {
  const status = await invoke<Status>('get_status');
  const frame = $<HTMLIFrameElement>('#browse-frame');
  const notice = $('#browse-unavailable');
  const bar = $('#browse-bar');
  if (status.kiwix_running) {
    // Only land on the library's front page when nothing is open yet; a
    // source someone just clicked must not be replaced by it.
    if (!frame.src || frame.src === 'about:blank') {
      frame.src = await invoke<string>('kiwix_url');
      $('#browse-where').textContent = '';
    }
    frame.hidden = false;
    bar.hidden = false;
    notice.style.display = 'none';
  } else {
    frame.hidden = true;
    bar.hidden = true;
    notice.style.display = '';
  }
}

$('#browse-home').addEventListener('click', async () => {
  $<HTMLIFrameElement>('#browse-frame').src = await invoke<string>('kiwix_url');
  $('#browse-where').textContent = '';
});

/* ------------------------------------------------------------------ setup */

function fmt(n: number) {
  return n.toLocaleString();
}

async function refreshSetup() {
  try {
    const hw = await invoke<Hardware>('get_hardware');
    const dl = $('#hardware');
    dl.textContent = '';
    const add = (k: string, v: string) => {
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

  const cfg = await invoke<Config>('get_config');
  $<HTMLInputElement>('#library-dir').value = cfg.library_dir ?? '';
  $<HTMLInputElement>('#chat-model').value = cfg.chat_model ?? '';
  $<HTMLInputElement>('#reranker-model').value = cfg.reranker_model ?? '';
  $<HTMLInputElement>('#kiwix-exe').value = cfg.kiwix_exe ?? '';
  $<HTMLInputElement>('#llama-exe').value = cfg.llama_exe ?? '';
  $<HTMLInputElement>('#autostart').checked = cfg.autostart;

  void loadPlan();

  const st = await invoke<Status>('get_status');
  const sp = await invoke<Space | null>('library_space').catch(() => null);
  const spaceLine = sp ? ` ${gb(sp.library_bytes)} on disk, ${gb(sp.free_bytes)} free on ${sp.volume}.` : '';
  $('#library-summary').textContent = st.documents
    ? `${fmt(st.documents)} documents, ${fmt(st.pages)} pages, ${fmt(st.words)} words indexed.${spaceLine}`
    : st.library_dir
      ? `Library folder set, but nothing is indexed yet.${spaceLine}`
      : 'No library configured.';
}

async function pickInto(id: string, directory: boolean, title: string) {
  const chosen = await open({ directory, multiple: false, title });
  if (typeof chosen === 'string') $<HTMLInputElement>(id).value = chosen;
}

$('#pick-library').addEventListener('click', () =>
  pickInto('#library-dir', true, 'Choose your library folder'),
);
$('#pick-kiwix').addEventListener('click', () =>
  pickInto('#kiwix-exe', false, 'Choose the kiwix-serve program'),
);
$('#pick-llama').addEventListener('click', () =>
  pickInto('#llama-exe', false, 'Choose the llama-server program'),
);
$('#pick-chat').addEventListener('click', () => pickInto('#chat-model', false, 'Choose a chat model'));
$('#pick-reranker').addEventListener('click', () =>
  pickInto('#reranker-model', false, 'Choose a reranker model'),
);

function statusMsg(text: string, isError = false) {
  const s = $('#setup-status');
  s.textContent = text;
  s.className = isError ? 'status err' : 'status';
}

$('#save-config').addEventListener('click', async () => {
  const nz = (v: string) => (v.trim() ? v.trim() : null);
  const current = await invoke<Config>('get_config');
  const config: Config = {
    ...current,
    library_dir: nz($<HTMLInputElement>('#library-dir').value),
    kiwix_exe: nz($<HTMLInputElement>('#kiwix-exe').value),
    llama_exe: nz($<HTMLInputElement>('#llama-exe').value),
    chat_model: nz($<HTMLInputElement>('#chat-model').value),
    reranker_model: nz($<HTMLInputElement>('#reranker-model').value),
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
  // An unhandled rejection here left the button looking dead with no message
  // while the model stayed resident on the GPU. Errors must reach the person.
  try {
    await invoke('stop_services');
    statusMsg('Stopped. The GPU is free again.');
  } catch (e) {
    statusMsg(`Could not stop services: ${String(e)}`, true);
  }
  await refreshServiceDots();
});

void listen<string>('service-progress', (e) => statusMsg(e.payload));
void listen<string>('index-progress', (e) => statusMsg(e.payload));

/* --------------------------------------------------------------- catalogue */

interface ContentItem {
  id: string;
  title: string;
  description: string;
  category: string;
  size_bytes: number;
  installed: boolean;
  bundles: string[];
  /** The kiwix book id: the archive filename without .zim. */
  filename?: string;
}
interface Space {
  library_bytes: number;
  extra_bytes: number;
  free_bytes: number;
  volume: string;
}
interface Catalogue {
  items: ContentItem[];
  library_dir: string | null;
  space: Space | null;
}

/** "Library 1.3 GB · 2.8 TB free on E:" with a fill meter. */
function renderSpace(target: HTMLElement, sp: Space | null) {
  target.textContent = '';
  if (!sp) {
    target.hidden = true;
    return;
  }
  target.hidden = false;
  const used = sp.library_bytes + sp.extra_bytes;
  const total = used + sp.free_bytes;
  const low = sp.free_bytes < 2 * 1073741824;
  target.classList.toggle('low', low);
  const line = el('div');
  line.append(el('strong', undefined, gb(sp.library_bytes)));
  line.append(document.createTextNode(' in your library'));
  if (sp.extra_bytes > 0) line.append(document.createTextNode(` · ${gb(sp.extra_bytes)} in extra folders`));
  target.append(line);
  const free = el('div');
  free.append(el('strong', undefined, gb(sp.free_bytes)));
  free.append(document.createTextNode(` free on ${sp.volume}${low ? ' — getting tight' : ''}`));
  target.append(free);
  const meter = el('div', 'meter');
  const fill = el('div');
  fill.style.width = total > 0 ? `${Math.min(100, Math.round((used / total) * 100))}%` : '0%';
  meter.append(fill);
  target.append(meter);
}

const CATEGORY_NAMES: Record<string, string> = {
  survival: 'Survival and post-disaster',
  medicine: 'Medicine',
  making: 'Making, growing and repairing',
  reference: 'Reference',
};

let fetchingId: string | null = null;

/**
 * The two shelves of the Library tab: what is here (click to read) and what
 * could be (click to fetch). Grouped by category within each.
 */
async function refreshCatalogue() {
  const have = $('#shelf-have');
  const get = $('#shelf-get');
  let cat: Catalogue;
  try {
    cat = await invoke<Catalogue>('list_content');
  } catch (e) {
    have.textContent = String(e);
    return;
  }
  have.textContent = '';
  get.textContent = '';
  renderSpace($('#shelf-space'), cat.space);
  const free = cat.space?.free_bytes ?? Infinity;

  const render = (target: HTMLElement, items: ContentItem[], installed: boolean) => {
    let lastCategory = '';
    for (const it of items) {
      if (it.category !== lastCategory) {
        target.append(el('div', 'cat', CATEGORY_NAMES[it.category] ?? it.category));
        lastCategory = it.category;
      }
      const row = el('div', 'book');
      row.dataset.id = it.filename ?? it.id;
      row.title = it.description ? `${it.description}${it.bundles.length ? ` — in ${it.bundles.join(', ')}` : ''}` : '';
      row.append(el('span', 't', it.title));
      row.append(el('span', 'size', gb(it.size_bytes)));
      if (installed) {
        row.addEventListener('click', () => void openBook(it.filename ?? it.id, it.title));
      } else if (it.size_bytes > free) {
        // Say so here, where the decision is made, not at 98% of a download.
        row.append(el('span', 'nofit', `needs ${gb(it.size_bytes - free)} more`));
        row.classList.add('nofit');
        row.title = `Not enough free space on ${cat.space?.volume ?? 'the drive'} for this.`;
      } else {
        row.append(el('span', 'get', 'Fetch'));
        if (!cat.library_dir || fetchingId !== null) row.classList.add('busy');
        else row.addEventListener('click', () => void fetchOne(it));
      }
      target.append(row);
    }
  };

  const mine = cat.items.filter((i) => i.installed);
  const rest = cat.items.filter((i) => !i.installed);
  if (!cat.library_dir) {
    have.append(el('p', 'empty-note', 'Choose a library folder in Settings first.'));
  } else if (!mine.length) {
    have.append(el('p', 'empty-note', 'Nothing yet. Fetch something below.'));
  }
  render(have, mine, true);
  if (!rest.length) get.append(el('p', 'empty-note', 'You have everything in the catalogue.'));
  render(get, rest, false);
}

async function fetchOne(it: ContentItem) {
  if (fetchingId) return;
  fetchingId = it.id;
  document.querySelectorAll('#shelf-get .book').forEach((b) => b.classList.add('busy'));
  const prog = $('#cat-progress');
  prog.hidden = false;
  $('#cat-label').textContent = `Fetching ${it.title}…`;
  $<HTMLElement>('#cat-fill').style.width = '0%';
  try {
    const msg = await invoke<string>('fetch_source', { id: it.id });
    $('#cat-label').textContent = msg;
    $<HTMLElement>('#cat-fill').style.width = '100%';
    statusMsg(msg);
  } catch (e) {
    $('#cat-label').textContent = `Stopped: ${String(e)}`;
    statusMsg(String(e), true);
  } finally {
    fetchingId = null;
    await refreshCatalogue();
    await refreshSetup();
  }
}

// The catalogue shares the wizard's progress events; route them to whichever
// panel is active.
void listen<FetchProgress>('fetch-progress', (e) => {
  if (!fetchingId) return;
  const p = e.payload;
  const frac = p.total > 0 ? p.received / p.total : null;
  const size = p.total > 1 ? ` ${gb(p.received)} / ${gb(p.total)}` : '';
  $('#cat-label').textContent = `${p.item}${p.note ? ` — ${p.note}` : ''}${size}`;
  $<HTMLElement>('#cat-fill').style.width = frac === null ? '100%' : `${Math.round(frac * 100)}%`;
});
void listen<string>('index-progress', (e) => {
  if (fetchingId) $('#cat-label').textContent = e.payload;
});

/* ------------------------------------------------------------------ evals */

interface CheckResult {
  check: string;
  passed: boolean;
  detail: string;
}
interface CaseResult {
  id: string;
  capability: string;
  horizon: string;
  prompt: string;
  passed: boolean;
  checks: CheckResult[];
  answer: string;
  basis: string;
  sources: string[];
  calculations: string[];
  seconds: number;
  error: string | null;
}
interface EvalRun {
  id: string;
  started: string;
  model: string;
  reranker: boolean;
  label: string;
  cases: CaseResult[];
  passed: number;
  total: number;
  by_capability: [string, number, number][];
  by_horizon: [string, number, number][];
  seconds: number;
}

const CAP_NAMES: Record<string, string> = {
  retrieve_pdf: 'Retrieve from PDFs',
  retrieve_article: 'Retrieve from articles',
  calculate: 'Calculate',
  reason: 'Reason',
  plan: 'Plan',
  refuse: 'Refuse / label',
};
const HORIZON_NAMES: Record<string, string> = {
  month: 'First month',
  six_months: 'Six months',
  year: 'A year',
  five_years: 'Five years',
  any: 'Any time',
};

let evalRuns: EvalRun[] = [];
let evalRunning = false;

interface ModelFile {
  path: string;
  name: string;
  size_bytes: number;
  current: boolean;
}

/** Fill the model picker: the models folder's GGUFs, current one first. */
async function refreshEvalModels(extra?: string) {
  const sel = $<HTMLSelectElement>('#eval-model');
  const keep = extra ?? sel.value;
  let models: ModelFile[] = [];
  try {
    models = await invoke<ModelFile[]>('list_models');
  } catch {
    /* no models folder yet */
  }
  sel.textContent = '';
  for (const m of models) {
    const o = document.createElement('option');
    o.value = m.path;
    o.textContent = `${m.name} (${gb(m.size_bytes)})${m.current ? ' — configured' : ''}`;
    sel.append(o);
  }
  if (extra && !models.some((m) => m.path === extra)) {
    const o = document.createElement('option');
    o.value = extra;
    o.textContent = extra.replace(/^.*[\\/]/, '');
    sel.append(o);
  }
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

$('#eval-pick-model').addEventListener('click', async () => {
  const chosen = await open({ directory: false, multiple: false, title: 'Choose a chat model (.gguf)' });
  if (typeof chosen === 'string') await refreshEvalModels(chosen);
});

function runName(r: EvalRun) {
  const d = new Date(Number(r.id) * 1000);
  const when = isNaN(d.getTime()) ? r.id : d.toLocaleString();
  return `${when} — ${r.label || r.model} — ${r.passed}/${r.total}`;
}

async function refreshEvals() {
  void refreshEvalModels();
  try {
    evalRuns = await invoke<EvalRun[]>('list_eval_runs');
  } catch (e) {
    $('#eval-summary').textContent = String(e);
    return;
  }
  const a = $<HTMLSelectElement>('#eval-a');
  const b = $<HTMLSelectElement>('#eval-b');
  const keepA = a.value;
  const keepB = b.value;
  a.textContent = '';
  b.textContent = '';
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '— no comparison —';
  b.append(none);
  for (const r of evalRuns) {
    for (const sel of [a, b]) {
      const o = document.createElement('option');
      o.value = r.id;
      o.textContent = runName(r);
      sel.append(o);
    }
  }
  if (evalRuns.length) {
    a.value = evalRuns.some((r) => r.id === keepA) ? keepA : evalRuns[0].id;
    b.value = evalRuns.some((r) => r.id === keepB) ? keepB : '';
  }
  renderEvalSummary();
}

function pct(p: number, t: number) {
  return t ? `${Math.round((p / t) * 100)}%` : '—';
}

function renderEvalSummary() {
  const box = $('#eval-summary');
  box.textContent = '';
  const A = evalRuns.find((r) => r.id === $<HTMLSelectElement>('#eval-a').value);
  const B = evalRuns.find((r) => r.id === $<HTMLSelectElement>('#eval-b').value);
  if (!A) {
    box.append(el('p', 'muted', 'No runs yet. Run the benchmark above.'));
    return;
  }

  const head = el('div', 'headline', `${A.passed}/${A.total} passed`);
  box.append(head);
  box.append(
    el('div', 'sub', `${A.model}${A.reranker ? ' · reranker on' : ' · no reranker'} · ${Math.round(A.seconds)}s${B ? `   vs   ${B.model}: ${B.passed}/${B.total}` : ''}`),
  );

  // Two small tables: by capability and by horizon, with the comparison
  // run's figure alongside where one is selected.
  const grid = el('div', 'grid');
  const table = (title: string, rows: [string, number, number][], other: [string, number, number][] | undefined, names: Record<string, string>) => {
    const t = document.createElement('table');
    const tr = document.createElement('tr');
    for (const h of [title, 'A', ...(other ? ['B', 'Δ'] : [])]) {
      const th = document.createElement('th');
      th.textContent = h;
      tr.append(th);
    }
    t.append(tr);
    for (const [k, p, n] of rows) {
      const r = document.createElement('tr');
      const c1 = document.createElement('td');
      c1.textContent = names[k] ?? k;
      const c2 = document.createElement('td');
      c2.className = 'num';
      c2.textContent = `${p}/${n} (${pct(p, n)})`;
      r.append(c1, c2);
      if (other) {
        const o = other.find((x) => x[0] === k);
        const c3 = document.createElement('td');
        c3.className = 'num';
        c3.textContent = o ? `${o[1]}/${o[2]}` : '—';
        const c4 = document.createElement('td');
        c4.className = 'num';
        if (o) {
          const d = p - o[1];
          c4.textContent = d === 0 ? '=' : d > 0 ? `+${d}` : `${d}`;
          c4.classList.add(d > 0 ? 'better' : d < 0 ? 'worse' : '');
        }
        r.append(c3, c4);
      }
      t.append(r);
    }
    return t;
  };
  for (const t of [
    table('Capability', A.by_capability, B?.by_capability, CAP_NAMES),
    table('Horizon', A.by_horizon, B?.by_horizon, HORIZON_NAMES),
  ]) {
    const w = el('div', 'tablewrap');
    w.append(t);
    grid.append(w);
  }
  box.append(grid);

  // Every case, with the comparison column; click a row for the answer.
  const t = document.createElement('table');
  const hr = document.createElement('tr');
  for (const h of ['Case', 'Capability', 'Horizon', 'A', ...(B ? ['B'] : []), 's']) {
    const th = document.createElement('th');
    th.textContent = h;
    hr.append(th);
  }
  t.append(hr);
  for (const c of A.cases) {
    const r = document.createElement('tr');
    r.className = 'case';
    const cells = [c.id, CAP_NAMES[c.capability] ?? c.capability, HORIZON_NAMES[c.horizon] ?? c.horizon];
    for (const v of cells) {
      const td = document.createElement('td');
      td.textContent = v;
      r.append(td);
    }
    const pa = document.createElement('td');
    pa.className = c.passed ? 'pass' : 'fail';
    pa.textContent = c.passed ? 'pass' : 'fail';
    r.append(pa);
    if (B) {
      const bc = B.cases.find((x) => x.id === c.id);
      const pb = document.createElement('td');
      pb.className = bc ? (bc.passed ? 'pass' : 'fail') : '';
      pb.textContent = bc ? (bc.passed ? 'pass' : 'fail') : '—';
      r.append(pb);
    }
    const ts = document.createElement('td');
    ts.className = 'num';
    ts.textContent = c.seconds.toFixed(0);
    r.append(ts);
    r.addEventListener('click', () => showEvalCase(c, B?.cases.find((x) => x.id === c.id)));
    t.append(r);
  }
  // The case table is the wide one; it scrolls inside its own box rather
  // than dragging the whole page sideways.
  const wrap = el('div', 'tablewrap');
  wrap.append(t);
  box.append(wrap);
}

function showEvalCase(c: CaseResult, other?: CaseResult) {
  const card = $('#eval-detail-card');
  const box = $('#eval-detail');
  card.hidden = false;
  $('#eval-detail-title').textContent = `${c.id}: ${c.prompt}`;
  box.textContent = '';
  const one = (r: CaseResult, name: string) => {
    const w = el('div');
    w.append(el('div', r.passed ? 'pass' : 'fail', `${name}: ${r.passed ? 'pass' : 'fail'} · basis ${r.basis || '—'} · ${r.seconds.toFixed(0)}s`));
    for (const ch of r.checks) {
      w.append(el('div', `check ${ch.passed ? 'pass' : 'fail'}`, `${ch.passed ? '✓' : '✗'} ${ch.check}: ${ch.detail}`));
    }
    if (r.calculations.length) {
      const ul = el('ul', 'plain');
      for (const x of r.calculations) ul.append(el('li', undefined, x));
      w.append(ul);
    }
    if (r.sources.length) {
      const ul = el('ul', 'plain');
      for (const s of r.sources) ul.append(el('li', undefined, s));
      w.append(ul);
    }
    const ans = el('div', 'answer-box');
    renderAnswer(ans, r.error ?? r.answer);
    w.append(ans);
    return w;
  };
  if (other) {
    const g = el('div', 'grid');
    g.append(one(c, 'A'));
    g.append(one(other, 'B'));
    box.append(g);
  } else {
    box.append(one(c, 'A'));
  }
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('#eval-a').addEventListener('change', renderEvalSummary);
$('#eval-b').addEventListener('change', renderEvalSummary);

void listen<{ index: number; total: number; id: string; passed: boolean | null }>('eval-progress', (e) => {
  const p = e.payload;
  $<HTMLElement>('#eval-fill').style.width = `${Math.round(((p.passed === null ? p.index - 1 : p.index) / p.total) * 100)}%`;
  $('#eval-status').textContent =
    p.passed === null ? `${p.index}/${p.total}  ${p.id}…` : `${p.index}/${p.total}  ${p.id}: ${p.passed ? 'pass' : 'fail'}`;
});

$('#eval-run').addEventListener('click', async () => {
  if (evalRunning) return;
  evalRunning = true;
  const btn = $('#eval-run');
  btn.setAttribute('disabled', 'true');
  const prog = $('#eval-progress');
  prog.hidden = false;
  $('#eval-status').textContent = 'Starting…';
  $<HTMLElement>('#eval-fill').style.width = '0%';
  // Model swaps narrate through the same channel the services use.
  const offProgress = await listen<string>('service-progress', (e) => {
    $('#eval-status').textContent = e.payload;
  });
  try {
    const model = $<HTMLSelectElement>('#eval-model').value;
    const modelName = model.replace(/^.*[\\/]/, '').replace(/\.gguf$/i, '');
    const label = $<HTMLInputElement>('#eval-label').value.trim() || modelName;
    const only = $<HTMLSelectElement>('#eval-only').value;
    const run = await invoke<EvalRun>('run_evals', { label, only, model });
    $('#eval-status').textContent = `Done: ${run.passed}/${run.total} passed in ${Math.round(run.seconds)}s.`;
    $<HTMLElement>('#eval-fill').style.width = '100%';
    await refreshEvals();
    $<HTMLSelectElement>('#eval-a').value = run.id;
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

/* ----------------------------------------------------------- extra folders */

async function refreshExtraDirs() {
  const box = $('#extra-dirs');
  const cfg = await invoke<Config & { extra_dirs?: string[] }>('get_config').catch(() => null);
  box.textContent = '';
  const dirs = cfg?.extra_dirs ?? [];
  if (!dirs.length) {
    box.append(el('p', 'hint', 'No extra folders yet.'));
    return;
  }
  for (const d of dirs) {
    const row = el('div', 'item');
    row.append(el('span', 'path', d));
    const rm = el('button', undefined, 'Remove') as HTMLButtonElement;
    rm.type = 'button';
    rm.addEventListener('click', async () => {
      rm.disabled = true;
      try {
        statusMsg(await invoke<string>('remove_content_dir', { path: d }));
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
  const chosen = await open({ directory: true, multiple: false, title: 'Add a folder of content' });
  if (typeof chosen !== 'string') return;
  const btn = $('#add-dir');
  btn.setAttribute('disabled', 'true');
  statusMsg(`Adding ${chosen}…`);
  try {
    statusMsg(await invoke<string>('add_content_dir', { path: chosen }));
  } catch (e) {
    statusMsg(String(e), true);
  } finally {
    btn.removeAttribute('disabled');
    await refreshExtraDirs();
    await refreshSetup();
  }
});

/* ------------------------------------------------------------------ wizard */

interface PlanItem {
  id: string;
  title: string;
  size_bytes: number;
}
interface SetupPlan {
  hardware: Hardware;
  chat_model: PlanItem | null;
  reranker: PlanItem | null;
  tools: PlanItem[];
  bundles: PlanItem[];
}
interface FetchProgress {
  item: string;
  received: number;
  total: number;
  note: string;
}

// Binary units, matching scripts/fetch.js --list, so the app and the command
// line never quote two different sizes for the same file.
function gb(n: number) {
  const g = n / 1073741824;
  if (g >= 1000) return `${(g / 1024).toFixed(1)} TB`;
  return g >= 1 ? `${g.toFixed(g >= 10 ? 0 : 1)} GB` : `${Math.round(n / 1048576)} MB`;
}

let plan: SetupPlan | null = null;

async function loadPlan() {
  const dl = $('#wiz-plan');
  try {
    plan = await invoke<SetupPlan>('setup_plan');
  } catch (e) {
    dl.textContent = String(e);
    return;
  }
  dl.textContent = '';
  const add = (k: string, v: string) => {
    dl.append(el('dt', undefined, k));
    dl.append(el('dd', undefined, v));
  };
  add('This machine', `${plan.hardware.accelerator} — ${plan.hardware.tier} tier`);
  add('Helper programs', plan.tools.map((t) => `${t.title} (${gb(t.size_bytes)})`).join(', '));
  add('Reranker', plan.reranker ? `${plan.reranker.title} (${gb(plan.reranker.size_bytes)})` : '—');
  add(
    'Chat model',
    plan.chat_model
      ? `${plan.chat_model.title} (${gb(plan.chat_model.size_bytes)})`
      : 'none fits this machine',
  );

  const sel = $<HTMLSelectElement>('#wiz-bundle');
  sel.textContent = '';
  for (const b of plan.bundles) {
    const o = document.createElement('option');
    o.value = b.id;
    o.textContent = `${b.title} — ${gb(b.size_bytes)}`;
    if (b.id === 'go-bag') o.selected = true;
    sel.append(o);
  }

  const cfg = await invoke<Config>('get_config');
  $<HTMLInputElement>('#wiz-library').value = cfg.library_dir ?? '';
  $<HTMLInputElement>('#wiz-models').value = (cfg as Config & { models_dir?: string }).models_dir ?? '';
  $<HTMLInputElement>('#wiz-tools').value =
    cfg.kiwix_exe?.replace(/[\\/][^\\/]*$/, '') ?? '';
}

function showProgress(label: string, frac: number | null) {
  const box = $('#wiz-progress');
  box.hidden = false;
  $('#wiz-label').textContent = label;
  $<HTMLElement>('#wiz-fill').style.width = frac === null ? '100%' : `${Math.round(frac * 100)}%`;
}

void listen<FetchProgress>('fetch-progress', (e) => {
  const p = e.payload;
  const frac = p.total > 0 ? p.received / p.total : null;
  const size = p.total > 0 ? ` ${gb(p.received)} / ${gb(p.total)}` : '';
  showProgress(`${p.item}${p.note ? ` — ${p.note}` : ''}${size}`, frac);
});

$('#wiz-pick-library').addEventListener('click', () =>
  pickInto('#wiz-library', true, 'Folder for the library'),
);
$('#wiz-pick-models').addEventListener('click', () =>
  pickInto('#wiz-models', true, 'Folder for models'),
);
$('#wiz-pick-tools').addEventListener('click', () =>
  pickInto('#wiz-tools', true, 'Folder for helper programs'),
);

/**
 * The whole first run, in order: programs, reranker, chat model, content,
 * services, index, fit. Each step is resumable and skips what is already
 * present, so a failure partway through costs a retry, not a restart.
 */
$('#wiz-go').addEventListener('click', async () => {
  if (!plan) return;
  const lib = $<HTMLInputElement>('#wiz-library').value.trim();
  const models = $<HTMLInputElement>('#wiz-models').value.trim();
  const tools = $<HTMLInputElement>('#wiz-tools').value.trim();
  if (!lib || !models || !tools) {
    statusMsg('Choose all three folders first.', true);
    return;
  }
  const btn = $('#wiz-go');
  btn.setAttribute('disabled', 'true');
  try {
    const current = await invoke<Config>('get_config');
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

    const bundle = $<HTMLSelectElement>('#wiz-bundle').value;
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

$('#build-index').addEventListener('click', async () => {
  const btn = $('#build-index');
  btn.setAttribute('disabled', 'true');
  statusMsg('Building the index…');
  try {
    statusMsg(await invoke<string>('build_index', { force: false }));
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
    statusMsg(`Fitted: ${await invoke<string>('fit_chat_model')} Saved.`);
  } catch (e) {
    statusMsg(String(e), true);
  } finally {
    btn.removeAttribute('disabled');
  }
});

/* ------------------------------------------------------------------ status */

const DOT_NAMES: Record<string, string> = {
  kiwix: 'Content server (kiwix-serve)',
  chat: 'Chat model (llama-server)',
  rerank: 'Reranker (llama-server)',
};

function setDot(svc: string, state: 'on' | 'off' | 'busy') {
  const d = document.querySelector(`.dot[data-svc="${svc}"]`) as HTMLElement | null;
  if (!d) return;
  d.classList.toggle('on', state === 'on');
  d.classList.toggle('busy', state === 'busy');
  const word = state === 'on' ? 'running' : state === 'busy' ? 'starting' : 'stopped';
  d.title = `${DOT_NAMES[svc] ?? svc}: ${word}`;
}

async function refreshServiceDots() {
  try {
    // The cheap call: process flags only. Polling the full status opened the
    // index on the main thread every few seconds and made scrolling stutter.
    const s = await invoke<{ kiwix_running: boolean; chat_running: boolean; reranker_running: boolean }>(
      'service_state',
    );
    setDot('kiwix', s.kiwix_running ? 'on' : 'off');
    setDot('chat', s.chat_running ? 'on' : 'off');
    setDot('rerank', s.reranker_running ? 'on' : 'off');
  } catch {
    /* the window can outlive the backend during shutdown; not worth reporting */
  }
}

/**
 * Bring the services up on launch, narrating in the Ask tab. A person who
 * opens Museion wants to ask something; making them find a button first is a
 * step they should not need. The chat model takes from twenty seconds to a
 * few minutes to load, so the progress is shown where they are looking.
 */
async function autostartServices() {
  const box = $('#boot-status');
  const say = (text: string, err = false) => {
    box.hidden = false;
    box.textContent = text;
    box.classList.toggle('err', err);
  };
  const off = await listen<string>('service-progress', (e) => say(e.payload));
  setDot('chat', 'busy');
  try {
    say('Starting services…');
    await invoke('start_services');
    say('Ready.');
    setTimeout(() => {
      box.hidden = true;
    }, 2500);
  } catch (e) {
    say(`Could not start services: ${String(e)}`, true);
  } finally {
    off();
    await refreshServiceDots();
  }
}

async function boot() {
  await refreshServiceDots();
  const st = await invoke<Status>('get_status');
  // A machine that is not set up gets the set-up screen and nothing else;
  // an ask box that cannot answer is not a welcome.
  if (st.configured) {
    showTab('ask');
  } else {
    showWelcome();
  }
  setInterval(refreshServiceDots, 6000);

  const cfg = await invoke<Config>('get_config').catch(() => null);
  if (st.configured && cfg?.autostart && !st.chat_running) {
    void autostartServices();
  }
}

$('#rerun-setup').addEventListener('click', (e) => {
  e.preventDefault();
  showWelcome();
});
$('#welcome-skip').addEventListener('click', (e) => {
  e.preventDefault();
  leaveWelcome('setup');
});

$('#autostart').addEventListener('change', async () => {
  const current = await invoke<Config>('get_config');
  await invoke('set_config', {
    config: { ...current, autostart: $<HTMLInputElement>('#autostart').checked },
  }).catch((e) => statusMsg(String(e), true));
});

void boot();
