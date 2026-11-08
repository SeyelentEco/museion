// The Agents tab: every agent and whether it can run on this computer; one
// agent's settings, prompt and notes to change (saved as your own copy); its
// runs, each of which can become a test case; and its tests.

import { invoke, listen } from './api.js';
import { chooseFile } from './chooser.js';
import { $, el } from './dom.js';
import { startChat } from './chats.js';
import { onTab, showTab } from './tabs.js';

let selected = null;

const RISK_WORDS = { read: 'reads', workspace: 'writes its own files', ask: 'asks you first' };
const SETTING_HELP = {
  max_rounds: 'Model turns before it must answer with what it has.',
  answer_tokens: 'Longest answer, in tokens.',
  keep_rounds: 'When the conversation gets long, the last rounds kept whole.',
  compact: 'Fold old search results away when the conversation gets long, instead of answering early.',
  memory: 'agent: notes and files shared by all its chats. chat: each chat its own. none: no folder.',
  confirm: 'ask: asks before changing anything outside its folder. all: also before writing its own files.',
};

async function refresh(flash = '') {
  let data;
  try {
    data = await invoke('list_agents');
  } catch (e) {
    $('#agent-items').textContent = '';
    $('#agent-items').append(el('li', 'empty-note', String(e)));
    return;
  }
  $('#agents-tier').textContent = `This computer: ${data.tier}${data.model ? ` · ${data.model}` : ''}`;
  const list = $('#agent-items');
  list.textContent = '';
  for (const a of data.agents) {
    const li = el('li');
    li.dataset.id = a.id;
    li.classList.toggle('active', a.id === selected);
    li.classList.toggle('off', !!a.cannot_run || !a.enabled);
    li.append(el('div', 't', a.name + (a.yours && a.shipped ? ' (changed)' : a.yours ? ' (yours)' : '')));
    li.append(el('div', 'meta', a.cannot_run ?? a.goal));
    if (a.last_test)
      li.append(el('div', 'meta', `Tests: ${a.last_test.passed} of ${a.last_test.total}${a.last_test.this_model ? '' : ' (another model)'}`));
    li.addEventListener('click', () => void show(a.id));
    list.append(li);
  }
  if (selected) await show(selected, flash);
}

function field(label, help, input) {
  const f = el('div', 'field');
  f.append(el('label', undefined, label));
  if (help) f.append(el('span', 'what', help));
  f.append(input);
  return f;
}

function input(type, value) {
  const i = el('input');
  i.type = type;
  if (type === 'checkbox') i.checked = !!value;
  else i.value = value ?? '';
  return i;
}

function select(options, value) {
  const s = el('select');
  for (const o of options) {
    const opt = el('option', undefined, o);
    opt.value = o;
    s.append(opt);
  }
  s.value = value;
  return s;
}

async function show(id, flash = '') {
  selected = id;
  document.querySelectorAll('#agent-items li').forEach((li) => li.classList.toggle('active', li.dataset.id === id));
  const a = await invoke('get_agent', { id });
  const s = a.settings;
  const box = $('#agent-detail');
  box.textContent = '';

  const head = el('div', 'card');
  head.append(el('h1', undefined, s.name));
  head.append(el('p', 'lede', s.goal));
  for (const p of a.problems) head.append(el('div', 'error', p));
  const actions = el('div', 'row');
  const chat = el('button', 'primary', 'New chat with it');
  chat.addEventListener('click', async () => {
    showTab('ask');
    await startChat(id);
  });
  const test = el('button', undefined, `Test this agent (${a.cases} cases)`);
  test.disabled = !a.cases;
  test.addEventListener('click', () => void runTests(id, test));
  const dup = el('button', undefined, 'Duplicate');
  dup.addEventListener('click', () => void duplicate(id, s.name));
  const exp = el('button', undefined, 'Export');
  exp.addEventListener('click', async () => alert(`Saved as ${await invoke('export_agent', { id }).catch((e) => String(e))}`));
  actions.append(chat, test, dup, exp);
  if (a.yours && a.shipped) {
    const revert = el('button', undefined, 'Go back to the shipped agent');
    revert.addEventListener('click', async () => {
      if (!confirm('Put your changes aside (kept in agents/.old) and use the shipped agent?')) return;
      await invoke('revert_agent', { id });
      await refresh();
    });
    actions.append(revert);
  }
  head.append(actions);
  if (a.last_test) head.append(el('p', 'muted', `Last test: ${a.last_test.passed} of ${a.last_test.total} on ${a.last_test.model}.`));
  head.append(el('div', 'test-progress'));
  box.append(head);

  // Settings, saved to your copy.
  const form = el('div', 'card');
  form.append(el('h2', undefined, 'Settings'));
  form.append(el('p', 'muted', `Changes are saved as your own copy in ${a.your_folder}; the shipped agent is never changed. Shown for a ${a.tier} computer.`));
  const inputs = {
    name: input('text', s.name),
    goal: input('text', s.goal),
    min_tier: select(a.tiers, s.min_tier),
    max_rounds: input('number', s.max_rounds),
    answer_tokens: input('number', s.answer_tokens),
    keep_rounds: input('number', s.keep_rounds),
    compact: input('checkbox', s.compact),
    memory: select(['agent', 'chat', 'none'], s.memory),
    confirm: select(['ask', 'all'], s.confirm),
    enabled: input('checkbox', s.enabled),
  };
  // The librarian's limits are set in tuning.toml (Settings › Advanced).
  for (const k of a.from_tuning ?? []) {
    inputs[k].disabled = true;
    inputs[k].title = 'Set in Settings › Advanced (tuning.toml)';
  }
  form.append(field('Name', '', inputs.name), field('Goal', 'One sentence: what it does for the person.', inputs.goal));
  form.append(field('Smallest computer it runs on', '', inputs.min_tier));
  const tools = el('div', 'checks');
  const toolBoxes = {};
  for (const t of a.tools) {
    const lab = el('label', 'check');
    const box2 = input('checkbox', s.tools.includes(t.name));
    toolBoxes[t.name] = box2;
    lab.append(box2, document.createTextNode(` ${t.name} `), el('span', `risk ${t.risk}`, RISK_WORDS[t.risk]));
    lab.append(el('span', 'hint', t.description));
    tools.append(lab);
  }
  form.append(field('Tools', 'What it may use. Tools that change anything outside its own folder always ask you first.', tools));
  const guardsBox = el('div', 'checks');
  const guardBoxes = {};
  for (const g of a.guards) {
    const lab = el('label', 'check');
    guardBoxes[g] = input('checkbox', s.guards.includes(g));
    lab.append(guardBoxes[g], document.createTextNode(` ${g}`));
    guardsBox.append(lab);
  }
  form.append(field('Guards', 'Checks on its answers: sums done by the calculator, citations given, tool calls made properly.', guardsBox));
  for (const k of ['max_rounds', 'answer_tokens', 'keep_rounds', 'compact', 'memory', 'confirm'])
    form.append(field(k.replace('_', ' '), SETTING_HELP[k], inputs[k]));
  form.append(field('Offered when starting a chat', '', inputs.enabled));
  let prompt = null;
  if (a.prompt_from === 'tuning') {
    form.append(field('Prompt', 'The librarian\'s prompt is in Settings › Advanced (tuning.toml).', el('pre', 'prompt-view', a.resolved_prompt)));
  } else {
    prompt = el('textarea', 'prompt-edit');
    prompt.value = a.prompt;
    prompt.rows = 16;
    form.append(field('Prompt', 'What it is told. The shared rules (agents/_rules.md) always come first.', prompt));
  }
  const save = el('button', 'primary', 'Save');
  const saved = el('span', 'muted', flash);
  save.addEventListener('click', async () => {
    const settings = {
      name: inputs.name.value, goal: inputs.goal.value, min_tier: inputs.min_tier.value,
      tools: Object.keys(toolBoxes).filter((k) => toolBoxes[k].checked),
      guards: Object.keys(guardBoxes).filter((k) => guardBoxes[k].checked),
      max_rounds: Number(inputs.max_rounds.value), answer_tokens: Number(inputs.answer_tokens.value),
      keep_rounds: Number(inputs.keep_rounds.value), compact: inputs.compact.checked,
      memory: inputs.memory.value, confirm: inputs.confirm.value, enabled: inputs.enabled.checked,
    };
    try {
      await invoke('save_agent', { id, settings, prompt: prompt ? prompt.value : null });
      await refresh(' Saved.');
    } catch (e) {
      saved.textContent = ` ${String(e)}`;
    }
  });
  const row = el('div', 'actions');
  row.append(save, saved);
  form.append(row);
  box.append(form);

  if (s.memory !== 'none') {
    const notes = el('div', 'card');
    notes.append(el('h2', undefined, 'Its notes'));
    notes.append(el('p', 'muted', 'What it remembers between chats (notes.md). Put in its prompt at the start of every run.'));
    const t = el('textarea', 'prompt-edit');
    t.value = a.notes;
    t.rows = 6;
    const b = el('button', undefined, 'Save notes');
    b.addEventListener('click', () => void invoke('save_notes', { id, text: t.value }).then(() => (b.textContent = 'Saved')));
    notes.append(t, b);
    box.append(notes);
  }

  const runs = el('div', 'card');
  runs.append(el('h2', undefined, 'Runs'));
  box.append(runs);
  const tests = el('div', 'card');
  tests.append(el('h2', undefined, 'Tests'));
  box.append(tests);
  void showRuns(id, runs, a.prompt_from === 'tuning');
  void showTests(id, tests);
}

async function showRuns(id, box, benchmark) {
  const runs = await invoke('agent_runs', { id }).catch(() => []);
  if (!runs.length) box.append(el('p', 'muted', 'No runs yet.'));
  for (const r of runs.slice(0, 15)) {
    const item = el('details', 'run');
    item.append(el('summary', undefined, `${r.goal.slice(0, 90)} — ${r.outcome}, ${r.steps} steps`));
    item.addEventListener('toggle', async () => {
      if (!item.open || item.dataset.loaded) return;
      item.dataset.loaded = '1';
      const steps = await invoke('read_run', { id, run: r.id });
      const pre = el('pre', 'run-log');
      pre.textContent = steps
        .map((s) => {
          const { t, kind, agent, depth, ...rest } = s;
          return `${'  '.repeat(depth ?? 0)}${kind}: ${JSON.stringify(rest).slice(0, 600)}`;
        })
        .join('\n');
      item.append(pre);
      if (!benchmark) item.append(caseForm(id, r.goal));
    });
    box.append(item);
  }
}

function caseForm(id, goal) {
  const f = el('form', 'case-form');
  f.append(el('h3', undefined, 'Save as a test case'));
  const name = input('text', '');
  name.placeholder = 'case id, like drum-dose';
  const any = input('text', '');
  any.placeholder = 'the answer must mention one of (comma-separated)';
  const not = input('text', '');
  not.placeholder = 'the answer must not contain (comma-separated)';
  const tools = input('text', '');
  tools.placeholder = 'tools it must use (comma-separated)';
  const go = el('button', undefined, 'Save case');
  go.type = 'submit';
  const out = el('span', 'muted');
  f.append(name, any, not, tools, go, out);
  const list = (i) => i.value.split(',').map((x) => x.trim()).filter(Boolean);
  f.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const path = await invoke('save_test_case', {
        id,
        case: { id: name.value.trim(), prompt: goal, expect_any: list(any), must_not_contain: list(not), expect_tool: list(tools) },
      });
      out.textContent = ` Saved in ${path}.`;
    } catch (err) {
      out.textContent = ` ${String(err)}`;
    }
  });
  return f;
}

async function showTests(id, box) {
  const runs = await invoke('agent_tests', { id }).catch(() => []);
  if (!runs.length) box.append(el('p', 'muted', 'Not tested yet.'));
  const [last, before] = runs;
  if (last) {
    box.append(el('p', undefined, `${last.passed} of ${last.total} passed on ${last.model}` + (before ? ` (before: ${before.passed} of ${before.total})` : '')));
    const table = el('table', 'cases');
    for (const c of last.cases) {
      const tr = el('tr');
      tr.append(el('td', c.passed ? 'pass' : 'fail', c.passed ? 'pass' : 'FAIL'), el('td', undefined, c.id));
      const prev = before?.cases.find((p) => p.id === c.id);
      tr.append(el('td', 'muted', prev ? (prev.passed ? 'passed before' : 'failed before') : ''));
      tr.append(el('td', 'muted', c.checks.filter((k) => !k.passed).map((k) => `${k.check}: ${k.detail}`).join('; ')));
      table.append(tr);
    }
    box.append(table);
  }
}

async function runTests(id, button) {
  button.disabled = true;
  const progress = $('#agent-detail .test-progress');
  const off = listen('agent-test-progress', (e) => {
    if (e.payload.agent !== id) return;
    const p = e.payload;
    progress.textContent = `Case ${p.index} of ${p.total}: ${p.id}${p.passed === null ? '…' : p.passed ? ' passed' : ' failed'}`;
  });
  try {
    const run = await invoke('test_agent', { id });
    progress.textContent = `${run.passed} of ${run.total} passed.`;
    await refresh();
  } catch (e) {
    progress.textContent = String(e);
  } finally {
    off();
    button.disabled = false;
  }
}

async function duplicate(id, name) {
  const newId = prompt(`An id for the copy of ${name} (lower-case words joined by -):`, `${id}-mine`);
  if (!newId) return;
  try {
    await invoke('new_agent', { id: newId, copy_of: id });
    selected = newId;
    await refresh();
  } catch (e) {
    alert(String(e));
  }
}

$('#agent-new').addEventListener('click', async () => {
  const id = prompt('An id for the new agent (lower-case words joined by -, like seed-keeper):');
  if (!id) return;
  const goal = prompt('In one sentence, what does it do for the person?') ?? '';
  try {
    await invoke('new_agent', { id, goal });
    selected = id;
    await refresh();
  } catch (e) {
    alert(String(e));
  }
});

$('#agent-import').addEventListener('click', async () => {
  const path = await chooseFile({ title: 'Choose an exported agent (.zip)', want: 'zip' });
  if (!path) return;
  try {
    const a = await invoke('import_agent', { path });
    selected = a.id;
    await refresh();
  } catch (e) {
    alert(String(e));
  }
});

onTab('agents', refresh);
