// The Museion page. Each tab lives in its own file; this one brings them
// together, starts the services when Museion opens, and handles Quit.
//
//   api.js       talking to Museion (museion/app.py)
//   ask.js       the Ask tab: a chat  answer.js   showing an answer
//   chats.js     the list of chats    agents.js   the Agents tab
//   library.js   the Library tab      chooser.js  the Choose… window
//   notes.js     the Notes tab        map.js      the Map tab
//   radio.js     the Radio tab and its card in Settings
//   evals.js     the Evals tab        dots.js     the header's service dots
//   settings.js  the Settings tab     tabs.js     switching tabs
//   home.js      your home area, in Settings and first-run set-up
//   welcome.js   first-run set-up     dom.js      small helpers

import { closeEvents, invoke, listen } from './api.js';
import { $, el } from './dom.js';
import { refreshServiceDots, setDot } from './dots.js';
import { showTab, showWelcome } from './tabs.js';
import './ask.js';
import './chats.js';
import './agents.js';
import './notes.js';
import './map.js';
import './radio.js';
import './library.js';
import './evals.js';
import './settings.js';
import './home.js';
import './welcome.js';
import './prepare.js';

/**
 * Bring the services up on launch, narrating in the Ask tab. A person who
 * opens Museion wants to ask something; making them find a button first is a
 * step they should not need. The chat model takes from twenty seconds to a
 * few minutes to load, so the progress is shown where they are looking.
 */
async function autostartServices() {
  const box = $('#boot-status');
  const say = (text, err = false) => {
    box.hidden = false;
    box.textContent = text;
    box.classList.toggle('err', err);
  };
  const off = listen('service-progress', (e) => say(e.payload));
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

// The browser cannot end Museion by closing a window, so there is a button
// for it. Closing the last Museion tab also ends it, a minute later.
$('#quit').addEventListener('click', async () => {
  if (!confirm('Quit Museion? The chat model and the other helpers it started stop too.')) return;
  await invoke('quit').catch(() => {});
  closeEvents();
  const app = $('#app');
  app.textContent = '';
  const box = el('div', 'stopped');
  box.append(el('h1', undefined, 'Museion has stopped'));
  box.append(el('p', 'muted', 'The helpers are stopped and the GPU is free. You can close this tab; start Museion again to carry on.'));
  app.append(box);
});

async function boot() {
  await refreshServiceDots();
  const st = await invoke('get_status');
  // A machine that is not set up gets the set-up screen and nothing else;
  // an ask box that cannot answer is not a welcome.
  if (st.configured) {
    showTab('ask');
  } else {
    showWelcome();
  }
  setInterval(refreshServiceDots, 6000);

  const cfg = await invoke('get_config').catch(() => null);
  if (st.configured && cfg?.autostart && !st.chat_running) void autostartServices();
}

void boot();
