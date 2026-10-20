import { api, get, post, session, bus } from './api.js';
import { h, svg, clear, toast, modal, field, $ } from './ui.js';
import * as monitor from './pages_monitor.js';
import * as config from './pages_config.js';
import * as admin from './pages_admin.js';

const ROUTES = {
  dashboard: { title: 'Dashboard', icon: '▦', page: monitor.dashboard },
  live: { title: 'Live', icon: '◉', page: monitor.live },
  trends: { title: 'Trends', icon: '∿', page: monitor.trends },
  alarms: { title: 'Alarms', icon: '⚠', page: monitor.alarms, badge: true },
  events: { title: 'Events', icon: '☰', page: monitor.events },
  devices: { title: 'Devices', icon: '▣', page: config.devices, group: 'Configuration' },
  tags: { title: 'Tags', icon: '#', page: config.tags },
  reports: { title: 'Reports', icon: '▤', page: admin.reports, group: 'Reporting' },
  gateways: { title: 'Gateways', icon: '⇄', page: admin.gateways, group: 'Administration', roles: ['admin', 'engineer'] },
  sites: { title: 'Sites & structure', icon: '⌂', page: admin.sites, roles: ['admin'] },
  users: { title: 'Users', icon: '☺', page: admin.users, roles: ['admin'] },
  settings: { title: 'Settings & backups', icon: '⚙', page: admin.settings, roles: ['admin'] },
};

const root = $('#app');
let cleanup = [];
const ctx = {
  every(ms, fn) { const id = setInterval(() => fn().catch(() => {}), ms); cleanup.push(() => clearInterval(id)); },
  onCleanup(fn) { cleanup.push(fn); },
  get user() { return session.user; },
  get canEdit() { return ['admin', 'engineer'].includes(session.user.role); },
  get isAdmin() { return session.user.role === 'admin'; },
};

const logo = () => svg('svg', { viewBox: '0 0 64 64' }, svg('rect', { x: 8, y: 8, width: 48, height: 48, rx: 8, fill: 'none', stroke: '#ffb000', 'stroke-width': 4 }),
  svg('path', { d: 'M18 21h28v7H36v17h-8V28H18z', fill: '#ffb000' }));

function showLogin(msg) {
  cleanup.forEach((f) => f()); cleanup = [];
  clear(root);
  const user = h('input', { autocomplete: 'username', required: true, autofocus: true });
  const pw = h('input', { type: 'password', autocomplete: 'current-password', required: true });
  const err = h('div', { class: 'banner err hide' });
  const go = async (e) => {
    e.preventDefault(); err.classList.add('hide');
    try {
      const r = await api('POST', '/api/auth/login', { username: user.value, password: pw.value });
      session.token = r.token; session.user = r.user; boot();
    } catch (ex) { err.textContent = ex.message; err.classList.remove('hide'); }
  };
  root.append(h('div', { class: 'login' }, h('form', { class: 'card', onsubmit: go },
    h('div', { class: 'brand' }, logo(), h('div', null, 'TBox', h('small', null, 'TECHNICAL POWER SYSTEMS'))),
    msg ? h('div', { class: 'banner' }, msg) : null, err,
    field('Username', user), field('Password', pw),
    h('button', { class: 'btn pri', style: 'width:100%', type: 'submit' }, 'Sign in'),
    h('p', { class: 'small mut' }, 'Industrial monitoring platform. Authorised users only.'))));
}

function forcePasswordChange() {
  const cur = h('input', { type: 'password', autocomplete: 'current-password' });
  const n1 = h('input', { type: 'password', autocomplete: 'new-password' });
  const n2 = h('input', { type: 'password', autocomplete: 'new-password' });
  const err = h('div', { class: 'banner err hide' });
  modal('Change your password', h('div', null, h('p', { class: 'mut' }, 'At least 10 characters with upper and lower case letters and a digit.'), err,
    field('Current password', cur), field('New password', n1), field('Repeat new password', n2)), { buttons: [
      { label: 'Sign out', onclick: () => { logout(); } },
      { label: 'Change password', cls: 'pri', close: false, onclick: async (e, close) => {
        if (n1.value !== n2.value) { err.textContent = 'The new passwords do not match.'; err.classList.remove('hide'); return false; }
        try { const r = await post('/api/auth/change-password', { current_password: cur.value, new_password: n1.value });
          session.token = r.token; session.user = r.user; close(); toast('Password changed', 'ok'); boot(); }
        catch (ex) { err.textContent = ex.message; err.classList.remove('hide'); }
        return false; } }] });
}

function logout() { session.token = null; session.user = null; showLogin(); }

async function boot() {
  if (!session.token) return showLogin();
  try { session.user = await get('/api/auth/me'); } catch { return showLogin(); }
  if (session.user.must_change_password) { showLogin(); return forcePasswordChange(); }
  buildShell();
  route();
}

let badge, content, titleEl, side;
function buildShell() {
  cleanup.forEach((f) => f()); cleanup = [];
  clear(root);
  const nav = h('div', { class: 'nav' });
  let lastGroup = null;
  for (const [key, r] of Object.entries(ROUTES)) {
    if (r.roles && !r.roles.includes(session.user.role)) continue;
    if (r.group && r.group !== lastGroup) { nav.append(h('div', { class: 'sep' }, r.group)); lastGroup = r.group; }
    nav.append(h('a', { href: '#/' + key, 'data-r': key, onclick: () => side.classList.remove('open') }, h('span', null, r.icon), r.title,
      r.badge ? (badge = h('span', { class: 'badge-n hide' }, '0')) : null));
  }
  side = h('aside', { class: 'side' }, h('div', { class: 'brand' }, logo(), h('div', null, 'TBox', h('small', null, 'TECHNICAL POWER SYSTEMS'))), nav);
  titleEl = h('h1', null, '');
  content = h('div', { class: 'content' });
  const top = h('div', { class: 'top' },
    h('button', { class: 'btn menu-btn', onclick: () => side.classList.toggle('open') }, '☰'), titleEl, h('span', { class: 'grow' }),
    h('span', { class: 'small mut' }, session.user.full_name || session.user.username, ' · ', session.user.role),
    h('button', { class: 'btn sm', onclick: changePw }, 'Password'), h('button', { class: 'btn sm', onclick: logout }, 'Sign out'));
  root.append(h('div', { class: 'shell' }, side, h('div', { class: 'main' }, top, content)));
  pollBadge(); setInterval(pollBadge, 5000);
}

function changePw() {
  const cur = h('input', { type: 'password' }), n1 = h('input', { type: 'password' });
  modal('Change password', h('div', null, field('Current password', cur), field('New password', n1)), { small: true, buttons: [
    { label: 'Cancel', onclick: () => {} },
    { label: 'Change', cls: 'pri', close: false, onclick: async (e, close) => {
      try { const r = await post('/api/auth/change-password', { current_password: cur.value, new_password: n1.value }); session.token = r.token; close(); toast('Password changed', 'ok'); }
      catch (ex) { toast(ex.message, 'err'); } return false; } }] });
}

async function pollBadge() {
  if (!badge || !session.token) return;
  try { const d = await get('/api/dashboard'); const n = d.unacked; badge.textContent = n; badge.classList.toggle('hide', !n); } catch { /* handled globally */ }
}

async function route() {
  if (!content || !session.user) return;
  cleanup.forEach((f) => f()); cleanup = [];
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [name, query] = hash.split('?');
  const r = ROUTES[name] && (!ROUTES[name].roles || ROUTES[name].roles.includes(session.user.role)) ? ROUTES[name] : ROUTES.dashboard;
  const key = ROUTES[name] === r ? name : 'dashboard';
  titleEl.textContent = r.title;
  document.title = r.title + ' - TBox';
  document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('on', a.dataset.r === key));
  clear(content);
  ctx.params = Object.fromEntries(new URLSearchParams(query || ''));
  try { await r.page(content, ctx); }
  catch (e) { content.append(h('div', { class: 'banner err' }, 'Could not load this page: ' + e.message)); }
}

window.addEventListener('hashchange', route);
bus.addEventListener('unauthorized', () => { if (session.token) { session.token = null; showLogin('Your session has expired. Please sign in again.'); } });
bus.addEventListener('pwchange', () => { forcePasswordChange(); });
boot();
