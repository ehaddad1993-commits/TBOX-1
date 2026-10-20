// Tiny DOM helpers. All text is inserted with textContent, never innerHTML (XSS-safe by construction).
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected' || k === 'required' || k === 'multiple') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}
export function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
// Native append()/replaceChildren() would print "null" or "[object ...]"; make them ignore null/false and flatten arrays.
for (const m of ['append', 'prepend', 'replaceChildren']) {
  const orig = Element.prototype[m];
  Element.prototype[m] = function (...kids) { return orig.apply(this, kids.flat(Infinity).filter((k) => k != null && k !== false)); };
}
const NS = 'http://www.w3.org/2000/svg';
export function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  add(el, kids);
  return el;
}
export const $ = (s, r = document) => r.querySelector(s);
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export function toast(msg, kind = 'info') {
  let box = $('.toasts');
  if (!box) { box = h('div', { class: 'toasts' }); document.body.append(box); }
  const t = h('div', { class: 'toast ' + kind }, msg);
  box.append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 7000 : 3500);
}

export function modal(title, body, { buttons = [], small = false, onClose } = {}) {
  const close = () => { ov.remove(); onClose && onClose(); };
  const ft = h('div', { class: 'ft' });
  for (const b of buttons) ft.append(h('button', { class: 'btn ' + (b.cls || ''), onclick: async (e) => {
    const r = await b.onclick(e, close); if (r !== false && b.close !== false) close(); } }, b.label));
  const ov = h('div', { class: 'overlay', onmousedown: (e) => { if (e.target === ov && !buttons.length) close(); } },
    h('div', { class: 'modal' + (small ? ' sm' : '') },
      h('div', { class: 'hd' }, h('h1', null, title), h('span', { class: 'sp' }), h('button', { class: 'btn sm', onclick: close }, '✕')),
      body, buttons.length ? ft : null));
  document.body.append(ov);
  return { close, el: ov };
}

export function confirmBox(title, text, okLabel = 'Confirm', danger = false) {
  return new Promise((res) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; res(v); } };
    modal(title, h('p', null, text), { small: true, onClose: () => finish(false), buttons: [
      { label: 'Cancel', onclick: () => finish(false) },
      { label: okLabel, cls: danger ? 'dng' : 'pri', onclick: () => finish(true) }] });
  });
}

export function field(label, input, hint) {
  return h('div', { class: 'f' }, h('label', null, label), input, hint ? h('div', { class: 'small mut' }, hint) : null);
}
export function select(options, value, attrs = {}) {
  const s = h('select', attrs);
  for (const o of options) {
    const [v, l] = Array.isArray(o) ? o : [o, o];
    s.append(h('option', { value: v, selected: String(v) === String(value) }, l));
  }
  return s;
}

export const STATUS_LABEL = { online: 'Online', offline: 'Offline', alarm: 'Alarm', comm_lost: 'Communication lost' };
export const statusPill = (s) => h('span', { class: 'pill s-' + s }, STATUS_LABEL[s] || s);
export const qualityPill = (q) => h('span', { class: 'pill q-' + q }, q === 'good' ? 'Good' : q === 'stale' ? 'Stale' : 'Bad');
export const prioPill = (p) => h('span', { class: 'pill p-' + p }, p[0].toUpperCase() + p.slice(1));
export const simBadge = () => h('span', { class: 'sim', title: 'Generated test data, not a real device' }, 'SIMULATED');

export function fmtNum(v, digits) {
  if (v == null || Number.isNaN(v)) return '—';
  const a = Math.abs(v);
  const d = digits ?? (a >= 1000 ? 0 : a >= 100 ? 1 : a >= 10 ? 2 : 3);
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
}
export function fmtTime(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleString(undefined, { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
export function ago(ms) {
  if (!ms) return 'never';
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return s + ' s ago';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  return Math.floor(s / 86400) + ' d ago';
}
export function table(cols, rows, empty = 'Nothing to show.') {
  const t = h('table', null, h('thead', null, h('tr', null, cols.map((c) => h('th', null, c.label)))),
    h('tbody', null, rows.length ? rows.map((r) => h('tr', null, cols.map((c) => h('td', { class: c.wrap ? 'wrap' : '' }, c.render(r))))) :
      h('tr', null, h('td', { colspan: cols.length, class: 'mut' }, empty))));
  return h('div', { class: 'tbl-wrap' }, t);
}
