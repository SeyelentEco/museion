// Small helpers every tab uses.

export const $ = (sel) => document.querySelector(sel);

export function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text) n.textContent = text;
  return n;
}

export function fmt(n) {
  return n.toLocaleString();
}

// Binary units, matching gb in museion/disk.py, so the page and the command
// line never quote two different sizes for the same file.
export function gb(n) {
  const g = n / 1073741824;
  if (g >= 1000) return `${(g / 1024).toFixed(1)} TB`;
  return g >= 1 ? `${g.toFixed(g >= 10 ? 0 : 1)} GB` : `${Math.round(n / 1048576)} MB`;
}

// The status line at the foot of Settings.
export function statusMsg(text, isError = false) {
  const s = $('#setup-status');
  s.textContent = text;
  s.className = isError ? 'status err' : 'status';
}
