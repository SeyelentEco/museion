// Your home area: find the town, then Museion downloads the weather records
// of the stations around it, puts its frost dates and hardiness zone in the
// almanac, and can make a map of it (museion/home_commands.py). The same
// form is in Settings and at the end of first-run set-up.

import { invoke, listen } from './api.js';
import { $, el } from './dom.js';
import { onTab, showTab } from './tabs.js';

const ABOUT_SIZE = 'It downloads, once, the list of weather stations (about 45 MB), the records of up to '
  + '40 stations near you (1–4 MB each) and a list of towns (11 MB): about 100–200 MB. About 20 MB of it is '
  + 'kept in the library, so it can be done again with no internet.';

let running = null; // the form whose set-up is under way: progress goes to it

/**
 * A home-area form inside `root`. With `button`, it has its own Set up
 * button (Settings); without, first-run set-up calls run() when it gets
 * to it. Returns { wanted(), run(tell), refresh() }.
 */
export function homeForm(root, { button = true } = {}) {
  let found = [];
  let chosen = null;
  let current = null; // the home area as set

  const now = el('p', 'small-print home-now');
  const find = el('form', 'row home-find');
  const where = el('input');
  where.type = 'text';
  where.placeholder = 'Your town, or 47.61, -122.20';
  where.setAttribute('aria-label', 'Your town');
  const findButton = el('button', 'small', 'Find');
  findButton.type = 'submit';
  find.append(where, findButton);
  const list = el('ul', 'agent-items home-found');
  const mapRow = el('label', 'small-print home-map');
  const mapCheck = el('input');
  mapCheck.type = 'checkbox';
  const mapSize = el('select');
  mapSize.setAttribute('aria-label', 'Map size');
  for (const k of [10, 25, 50]) {
    const o = el('option', undefined, `${k} km across`);
    o.value = String(k);
    mapSize.append(o);
  }
  mapRow.append(mapCheck, ' Also make a map of it, ', mapSize);
  const size = el('p', 'muted small-print', ABOUT_SIZE);
  const actions = el('div', 'row');
  const go = el('button', 'primary small', 'Set up my home area');
  go.type = 'button';
  go.disabled = true;
  const stop = el('button', 'small', 'Stop');
  stop.type = 'button';
  stop.hidden = true;
  actions.append(go, stop);
  const status = el('p', 'small-print home-status');
  root.textContent = '';
  root.append(now, find, list, mapRow, size);
  if (button) root.append(actions);
  root.append(status);

  const say = (text, cls = '') => {
    status.textContent = text;
    status.className = `small-print home-status ${cls}`;
  };

  function showFound() {
    list.textContent = '';
    for (const [i, f] of found.entries()) {
      const li = el('li', chosen === f ? 'chosen' : undefined);
      li.append(el('div', 't', f.name));
      li.append(el('div', 'meta', f.label));
      li.addEventListener('click', () => {
        chosen = found[i];
        showFound();
      });
      list.append(li);
    }
    go.disabled = !chosen;
  }

  async function search() {
    findButton.disabled = true;
    say('Looking…', 'muted');
    try {
      found = await invoke('find_place', { query: where.value });
      chosen = found[0] ?? null;
      say(found.length ? '' : 'Nothing found by that name. Try adding the county or country, or type a latitude and longitude.', 'muted');
      showFound();
    } finally {
      findButton.disabled = false;
    }
  }

  // A new name typed is a new search: what was picked for the old one
  // no longer stands.
  where.addEventListener('input', () => {
    if (!chosen && !found.length) return;
    chosen = null;
    found = [];
    showFound();
  });

  find.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await search();
    } catch (err) {
      say(String(err), 'err');
    }
  });

  async function refresh() {
    let h;
    try {
      h = await invoke('home_area');
    } catch (err) {
      now.textContent = String(err);
      return;
    }
    current = h.home;
    if (!h.home) {
      now.textContent = 'Not set yet. Your home area gives the almanac your frost dates and hardiness zone, '
        + 'and the agents your climate, with no internet. Setting it up needs the internet once.';
      return;
    }
    const e = h.estimate;
    const frosts = e
      ? ` Last spring frost about ${e.last_frost || 'none most years'}, first autumn frost about `
        + `${e.first_frost || 'none most years'}${e.zone ? `, hardiness zone ${e.zone}` : ''}.`
      : h.climate ? ' The climate table does not reach it.' : ' No weather records yet: set it up again with the internet.';
    now.textContent = `Your home area is ${h.home.name} (${h.home.lat.toFixed(2)}, ${h.home.lon.toFixed(2)}).${frosts}`;
    now.title = h.text || '';
    if (!where.value) where.value = h.home.name;
  }

  /** Set up the chosen place (or the first found for what was typed).
   * `tell` gets each step in words. */
  async function run(tell = (t) => say(t)) {
    if (!chosen && where.value.trim()) {
      await search();
      if (!chosen) throw `Nothing was found called “${where.value.trim()}”.`;
    }
    if (!chosen) throw 'Find your town first.';
    running = { tell, bytes: 0 };
    go.disabled = true;
    stop.hidden = false;
    try {
      const done = await invoke('set_up_home', {
        name: chosen.name, lat: chosen.lat, lon: chosen.lon,
        map_km: mapCheck.checked ? Number(mapSize.value) : 0,
      });
      await refresh();
      const e = done.estimate;
      const frosts = !e ? ''
        : done.done.frosts === 'filled' ? ' Its frost dates and zone are in your almanac.'
          : done.done.frosts.startsWith('kept') ? ' Your almanac keeps the frost dates you typed.'
            : '';
      const map = done.done.map ? ` The map ${done.done.map} is in the Map tab.` : '';
      const text = `Done: ${chosen.name} is your home area, with the records of ${done.done.stations} weather stations.${frosts}${map}`;
      say(text, 'good');
      return text;
    } finally {
      running = null;
      go.disabled = !chosen;
      stop.hidden = true;
    }
  }

  go.addEventListener('click', async () => {
    try {
      await run();
    } catch (err) {
      say(String(err), 'err');
    }
  });
  stop.addEventListener('click', () => {
    say('Stopping after this station…', 'muted');
    void invoke('stop_home');
  });

  // Whether the person gave a town (not just the one set already):
  // first-run set-up then sets it up.
  const wanted = () => Boolean(chosen || (where.value.trim() && where.value.trim() !== current?.name));

  return { wanted, run, refresh };
}

const steps = {
  stations: () => 'Downloading the list of weather stations…',
  station: (d, t, mb) => `Weather records: station ${Math.min(d + 1, t)} of ${t}${mb > 0 ? ` (${mb} MB downloaded)` : ''}…`,
  towns: () => 'Downloading the list of towns…',
  almanac: () => 'Putting the frost dates in your almanac…',
  map: () => 'Making the map…',
};

listen('home-progress', (e) => {
  if (!running) return;
  const { step, done, total } = e.payload;
  if (step === 'bytes') {
    running.bytes = done;
    return;
  }
  const words = steps[step];
  if (words) running.tell(words(done, total, Math.round(running.bytes / 1e6)));
});

listen('map-progress', (e) => {
  if (!running) return;
  const { step, done, total } = e.payload;
  if (step === 'downloading') running.tell(`Making the map: downloading piece ${done} of ${total}…`);
  else if (step === 'drawing') running.tell(`Making the map: drawing zoom level ${Math.min(done + 8, 14)} of 14…`);
});

// Settings has its own form, refreshed whenever the tab opens.
const settings = $('#home-settings');
if (settings) {
  const form = homeForm(settings);
  onTab('setup', form.refresh);
}

// The Coming up page's "set up your home area" link.
$('#climate-setup-link')?.addEventListener('click', (e) => {
  e.preventDefault();
  showTab('setup');
  $('#card-home')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
});
