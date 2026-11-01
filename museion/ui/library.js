// The Library tab: the shelves (what you have, what you could fetch) and the
// reader, which shows kiwix-serve's pages in a frame.

import { invoke, listen } from './api.js';
import { $, el, gb, statusMsg } from './dom.js';
import { refreshSetup } from './settings.js';
import { onTab, showTab } from './tabs.js';

/**
 * The address of a source: the article itself, or the PDF opened at the
 * cited page. Citations are only worth having if following one is easy, and
 * "page 129 of Where There Is No Doctor" should be one click.
 */
async function sourceUrl(s) {
  // A PDF found in a folder is not in any archive; Museion serves it itself.
  if (s.book.startsWith('folder:')) {
    return `/file?path=${encodeURIComponent(s.file)}${s.page > 0 ? `#page=${s.page}` : ''}`;
  }
  const base = await invoke('kiwix_url');
  const enc = (p) => p.split('/').map(encodeURIComponent).join('/');
  return s.page > 0
    ? `${base}/content/${enc(s.book)}/files/${enc(s.file)}#page=${s.page}`
    : `${base}/content/${enc(s.book)}/${enc(s.file)}`;
}

export async function openSource(s) {
  if (!s.book.startsWith('folder:')) {
    const status = await invoke('get_status');
    if (!status.kiwix_running) {
      statusMsg('The content server is not running; start services to open sources.', true);
      showTab('setup');
      return;
    }
  }
  showReader(await sourceUrl(s), s.page > 0 ? `${s.title} — page ${s.page}` : s.title);
  showTab('library');
}

function showReader(url, where) {
  const frame = $('#browse-frame');
  frame.src = url;
  frame.hidden = false;
  $('#browse-bar').hidden = false;
  $('#browse-unavailable').style.display = 'none';
  $('#browse-where').textContent = where;
}

/** Open an archive's front page in the reader. */
async function openBook(id, title) {
  const status = await invoke('get_status');
  if (!status.kiwix_running) {
    statusMsg('The content server is not running; start services to read.', true);
    return;
  }
  const base = await invoke('kiwix_url');
  showReader(`${base}/content/${encodeURIComponent(id)}/`, title);
  document.querySelectorAll('.book').forEach((b) => b.classList.toggle('active', b.dataset.id === id));
}

async function refreshBrowse() {
  const status = await invoke('get_status');
  const frame = $('#browse-frame');
  // A PDF from a content folder shows without the content server.
  const own = frame.src.startsWith(location.origin + '/file');
  if (status.kiwix_running || own) {
    // Only land on the library's front page when nothing is open yet; a
    // source someone just clicked must not be replaced by it.
    if (!frame.getAttribute('src')) showReader(await invoke('kiwix_url'), '');
    frame.hidden = false;
    $('#browse-bar').hidden = false;
    $('#browse-unavailable').style.display = 'none';
  } else {
    frame.hidden = true;
    $('#browse-bar').hidden = true;
    $('#browse-unavailable').style.display = '';
  }
}

// The reader's own history: a PDF opened from an archive's list goes back to
// the list. The frame is another origin, so its address cannot be read to
// relabel the bar; the label is cleared rather than left wrong.
$('#browse-back').addEventListener('click', () => {
  history.back();
  $('#browse-where').textContent = '';
});

$('#browse-home').addEventListener('click', async () => {
  $('#browse-frame').src = await invoke('kiwix_url');
  $('#browse-where').textContent = '';
});

/* --------------------------------------------------------------- catalogue */

/** "Library 1.3 GB · 2.8 TB free on E:" with a fill meter. */
function renderSpace(target, sp) {
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

const CATEGORY_NAMES = {
  survival: 'Survival and post-disaster',
  medicine: 'Medicine',
  making: 'Making, growing and repairing',
  reference: 'Reference',
};

let fetchingId = null;

/**
 * The two shelves of the Library tab: what is here (click to read) and what
 * could be (click to fetch). Grouped by category within each.
 */
async function refreshCatalogue() {
  const have = $('#shelf-have');
  const get = $('#shelf-get');
  let cat;
  try {
    cat = await invoke('list_content');
  } catch (e) {
    have.textContent = String(e);
    return;
  }
  have.textContent = '';
  get.textContent = '';
  renderSpace($('#shelf-space'), cat.space);
  const free = cat.space?.free_bytes ?? Infinity;

  const render = (target, items, installed) => {
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

async function fetchOne(it) {
  if (fetchingId) return;
  fetchingId = it.id;
  document.querySelectorAll('#shelf-get .book').forEach((b) => b.classList.add('busy'));
  $('#cat-progress').hidden = false;
  $('#cat-label').textContent = `Fetching ${it.title}…`;
  $('#cat-fill').style.width = '0%';
  try {
    const msg = await invoke('fetch_source', { id: it.id });
    $('#cat-label').textContent = msg;
    $('#cat-fill').style.width = '100%';
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

// The catalogue shares the wizard's progress events; only a fetch started
// here moves this bar.
listen('fetch-progress', (e) => {
  if (!fetchingId) return;
  const p = e.payload;
  const frac = p.total > 0 ? p.received / p.total : null;
  const size = p.total > 1 ? ` ${gb(p.received)} / ${gb(p.total)}` : '';
  $('#cat-label').textContent = `${p.item}${p.note ? ` — ${p.note}` : ''}${size}`;
  $('#cat-fill').style.width = frac === null ? '100%' : `${Math.round(frac * 100)}%`;
});
listen('index-progress', (e) => {
  if (fetchingId) $('#cat-label').textContent = e.payload;
});

onTab('library', refreshBrowse);
onTab('library', refreshCatalogue);
