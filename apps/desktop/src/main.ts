import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';

interface Source {
  n: number;
  title: string;
  author: string;
  page: number;
  book: string;
  snippet: string;
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
  if (name === 'browse') void refreshBrowse();
  if (name === 'setup') void refreshSetup();
}

document.querySelectorAll('.tab').forEach((t) =>
  t.addEventListener('click', () => showTab((t as HTMLElement).dataset.tab!)),
);

/* ------------------------------------------------------------------ ask */

let busy = false;

/**
 * Render an answer, turning [1] into a control that reveals the source it
 * points at. Citations are only worth having if following one is easy -- the
 * whole promise is that you can check the answer yourself.
 */
function renderAnswer(container: HTMLElement, text: string) {
  container.textContent = '';
  const parts = text.split(/(\[\d+(?:\]\[\d+)*\]|\[\d+(?:,\s*\d+)*\])/g);
  for (const part of parts) {
    const m = part.match(/^\[[\d,\s\]\[]+\]$/);
    if (m) {
      for (const num of part.match(/\d+/g) ?? []) {
        const c = el('span', 'cite', num);
        c.addEventListener('click', () => {
          const row = document.querySelector(`.source[data-n="${num}"]`);
          row?.scrollIntoView({ behavior: 'smooth', block: 'center' });
          row?.classList.add('highlight');
          setTimeout(() => row?.classList.remove('highlight'), 1600);
        });
        container.append(c);
      }
    } else if (part) {
      container.append(document.createTextNode(part));
    }
  }
}

async function askQuestion(question: string) {
  if (busy || !question.trim()) return;
  busy = true;
  $('#ask-send').setAttribute('disabled', 'true');
  $('#ask-empty')?.remove();

  const transcript = $('#transcript');
  const exchange = el('div', 'exchange');
  exchange.append(el('div', 'question', question));
  const answerEl = el('div', 'answer thinking', 'Searching the library…');
  exchange.append(answerEl);
  transcript.append(exchange);
  transcript.scrollTop = transcript.scrollHeight;

  let raw = '';
  let started = false;
  const unlisten = await listen<string>('answer-token', (e) => {
    if (!started) {
      started = true;
      answerEl.classList.remove('thinking');
      answerEl.textContent = '';
    }
    raw += e.payload;
    answerEl.textContent = raw;
    transcript.scrollTop = transcript.scrollHeight;
  });

  try {
    const sources = await invoke<Source[]>('ask', { question });
    renderAnswer(answerEl, raw);
    answerEl.classList.remove('thinking');

    if (sources.length) {
      const box = el('div', 'sources');
      box.append(el('h3', undefined, 'Sources'));
      for (const s of sources) {
        const row = el('div', 'source');
        row.setAttribute('data-n', String(s.n));
        row.append(el('span', 'n', String(s.n)));
        const body = el('div');
        body.append(el('div', 't', s.title));
        const meta = [s.author, `page ${s.page}`].filter(Boolean).join(' — ');
        body.append(el('div', 'meta', meta));
        row.append(body);
        box.append(row);
      }
      exchange.append(box);
    }
  } catch (err) {
    answerEl.className = 'error';
    answerEl.textContent = String(err);
  } finally {
    unlisten();
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

async function refreshBrowse() {
  const status = await invoke<Status>('get_status');
  const frame = $<HTMLIFrameElement>('#browse-frame');
  const notice = $('#browse-unavailable');
  if (status.kiwix_running) {
    const url = await invoke<string>('kiwix_url');
    if (frame.src !== url + '/') frame.src = url;
    frame.hidden = false;
    notice.style.display = 'none';
  } else {
    frame.hidden = true;
    notice.style.display = '';
  }
}

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

  void loadPlan();

  const st = await invoke<Status>('get_status');
  $('#library-summary').textContent = st.documents
    ? `${fmt(st.documents)} documents, ${fmt(st.pages)} pages, ${fmt(st.words)} words indexed.`
    : st.library_dir
      ? 'Library folder set, but nothing is indexed yet.'
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
  await invoke('stop_services');
  statusMsg('Stopped. The GPU is free again.');
  await refreshServiceDots();
});

void listen<string>('service-progress', (e) => statusMsg(e.payload));
void listen<string>('index-progress', (e) => statusMsg(e.payload));

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

function gb(n: number) {
  return n >= 1e9 ? `${(n / 1e9).toFixed(n >= 1e10 ? 0 : 1)} GB` : `${Math.round(n / 1e6)} MB`;
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

    showProgress('Starting services…', null);
    await invoke('start_services');

    showProgress('Building the index…', null);
    await invoke('build_index', { force: false });

    if (plan.chat_model && plan.hardware.tier !== 'Modest') {
      showProgress('Fitting the model to this GPU…', null);
      await invoke('fit_chat_model');
    }

    showProgress('Ready. Ask something.', 1);
    statusMsg('Set-up complete.');
    await refreshSetup();
    await refreshServiceDots();
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

function setDot(svc: string, state: 'on' | 'off' | 'busy') {
  const d = document.querySelector(`.dot[data-svc="${svc}"]`);
  if (!d) return;
  d.classList.toggle('on', state === 'on');
  d.classList.toggle('busy', state === 'busy');
}

async function refreshServiceDots() {
  try {
    const s = await invoke<Status>('get_status');
    setDot('kiwix', s.kiwix_running ? 'on' : 'off');
    setDot('chat', s.chat_running ? 'on' : 'off');
    setDot('rerank', s.reranker_running ? 'on' : 'off');
  } catch {
    /* the window can outlive the backend during shutdown; not worth reporting */
  }
}

async function boot() {
  await refreshServiceDots();
  const st = await invoke<Status>('get_status');
  // Send someone straight to the Library tab when there is nothing to ask yet,
  // rather than showing an ask box that cannot answer.
  showTab(st.configured ? 'ask' : 'setup');
  setInterval(refreshServiceDots, 4000);
}

void boot();
