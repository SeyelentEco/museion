// Showing an answer: light Markdown, built as page elements directly.

import { el } from './dom.js';

/**
 * Render an answer as light Markdown, building DOM nodes directly.
 *
 * No innerHTML anywhere: the text comes from a language model and is not to
 * be trusted as markup. Only what is recognised becomes structure; everything
 * else stays text. Covers what the model actually emits -- paragraphs,
 * headings, bullet and numbered lists with one level of nesting, quoted
 * passages (which may hold a list), bold, italic, inline code -- and turns [1] into a chip that reveals the source it
 * points at, because a citation is only worth having if following it is easy.
 */
export function renderAnswer(container, text) {
  container.textContent = '';
  const lines = text.replace(/\r\n?/g, '\n').split('\n');

  let para = [];
  let list = null; // { el, ordered, sub }

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
  // A > block, gathered whole and rendered as an answer of its own: models
  // quote a passage's list as "> - ...", which showed as the raw marks.
  let quote = null;
  const flushQuote = () => {
    if (quote) {
      const bq = el('blockquote');
      renderAnswer(bq, quote.join('\n'));
      container.append(bq);
      quote = null;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/, '');
    const indent = rawLine.match(/^\s*/)[0].length;

    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*(?:[-*•])\s+(.*)$/);
    const number = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const quoted = line.match(/^\s*>\s?(.*)$/);

    if (quoted) {
      flushPara();
      flushList();
      (quote ??= []).push(quoted[1]);
      continue;
    }
    flushQuote();
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
      const body = (bullet ?? number)[1];
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
      const target = list.sub?.lastElementChild ?? list.el.lastElementChild;
      target.append(document.createTextNode(' '));
      inline(target, line.trim());
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushQuote();
  flushPara();
}

/**
 * Inline markup: **bold**, *italic*, `code`, and [n] citation chips. An
 * italic span may not start or end with a space, so the multiplication in
 * "17 * 23 = **391**" stays a multiplication.
 */
function inline(target, text) {
  const tokens = text.split(
    /(\*\*[^*]+\*\*|`[^`]+`|(?<![*\w])\*(?![\s*])[^*\n]+(?<![\s*])\*(?!\w)|\[\d+(?:\]\s*,?\s*\[\d+)*\]|\[\d+(?:,\s*\d+)*\])/g,
  );
  for (const t of tokens) {
    if (!t) continue;
    if (t.startsWith('**') && t.endsWith('**')) {
      target.append(el('strong', undefined, t.slice(2, -2)));
    } else if (t.startsWith('`') && t.endsWith('`')) {
      target.append(el('code', undefined, t.slice(1, -1)));
    } else if (t.startsWith('*') && t.endsWith('*') && t.length > 2) {
      target.append(el('em', undefined, t.slice(1, -1)));
    } else if (/^\[[\d,\s\][]+\]$/.test(t)) {
      for (const num of t.match(/\d+/g) ?? []) {
        const c = el('span', 'cite', num);
        c.addEventListener('click', () => {
          // The chip's own answer: the nearest exchange holds its sources.
          const scope = c.closest('.exchange') ?? document;
          const row = scope.querySelector(`.source[data-n="${num}"]`);
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
