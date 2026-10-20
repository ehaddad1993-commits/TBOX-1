import { get, post, put, download, qs } from './api.js';
import { h, clear, toast, modal, field, select, statusPill, qualityPill, prioPill, simBadge, fmtNum, fmtTime, ago, table, confirmBox } from './ui.js';
import { gauge, lamp, lineChart, color } from './charts.js';
import { canWrite, openWrite } from './control.js';

// ------------------------------------------------------------------ shared widget rendering
export function defaultType(t) {
  if (t.data_type === 'bool') return 'status';
  if ((t.disp_min != null && t.disp_max != null) || t.hi != null || t.lo != null) return 'gauge';
  return 'value';
}

export function tagCard(t, type, extra) {
  type = type || defaultType(t);
  const q = t.no_data ? 'bad' : t.quality;
  const cls = 'card wcard' + (t.alarm ? ' alm' : '') + (q !== 'good' ? ' ' + q : '');
  const head = [h('div', { class: 'nm', title: t.device_name + ' / ' + t.name }, t.name), h('div', { class: 'small mut' }, t.device_name, ' ', t.simulated ? simBadge() : null)];
  let body;
  if (type === 'status') {
    body = h('div', { class: 'row', style: 'justify-content:center;margin:12px 0' }, lamp(t.value >= 0.5 && q === 'good'),
      h('span', { class: 'val', style: 'font-size:20px' }, t.no_data ? '—' : t.value >= 0.5 ? 'ON' : 'OFF'));
  } else if (type === 'gauge') {
    body = h('div', { style: 'width:100%;display:flex;flex-direction:column;align-items:center' }, gauge({ ...t, quality: q }),
      h('div', { class: 'val' }, t.no_data ? '—' : fmtNum(t.value), ' ', h('small', null, t.unit)));
  } else if (type === 'trend') {
    const box = h('div', { style: 'width:100%' });
    const now = Date.now(), from = now - 3600000;
    get('/api/tags/' + t.id + '/history?' + qs({ from, to: now, points: 120 })).then((r) =>
      box.append(lineChart([{ name: t.name, color: color(0), unit: t.unit, points: r.points, bucket: r.bucket_ms }], { from, to: now, height: 90, band: false }))).catch(() => {});
    body = h('div', { style: 'width:100%' }, h('div', { class: 'val', style: 'font-size:22px' }, t.no_data ? '—' : fmtNum(t.value), ' ', h('small', null, t.unit)), box);
  } else {
    body = h('div', { class: 'val', style: 'margin:14px 0' }, t.no_data ? '—' : fmtNum(t.value), ' ', h('small', null, t.unit));
  }
  const wbtn = canWrite(t) ? h('button', { class: 'btn sm', onclick: () => openWrite(t) }, '✎ Write') : null;
  const foot = h('div', { class: 'row small', style: 'justify-content:center' }, wbtn, qualityPill(q), t.ts ? h('span', { class: 'mut', title: fmtTime(t.ts) }, ago(t.ts)) : null,
    t.alarm ? h('span', { class: 'pill p-critical' }, t.alarm.toUpperCase()) : null);
  return h('div', { class: cls }, extra || null, ...head, body, foot);
}

// ------------------------------------------------------------------ dashboard
export async function dashboard(box, ctx) {
  const draw = async () => {
    const d = await get('/api/dashboard');
    const tiles = [['online', 'Online', 'var(--ok)'], ['alarm', 'Alarm', 'var(--bad)'], ['comm_lost', 'Communication lost', 'var(--lost)'], ['offline', 'Offline', 'var(--off)']]
      .map(([k, l, c]) => h('div', { class: 'card tile', onclick: () => (location.hash = '#/devices?status=' + k) },
        h('div', { class: 'n', style: 'color:' + c }, d.devices[k] || 0), h('div', { class: 'l' }, l)));
    const alarmCard = h('div', { class: 'card tile', onclick: () => (location.hash = '#/alarms') }, h('div', { class: 'n', style: 'color:' + (d.unacked ? 'var(--bad)' : 'var(--ok)') }, d.unacked),
      h('div', { class: 'l' }, 'Unacknowledged alarms'), h('div', { class: 'small mut' }, ['critical', 'high', 'medium', 'low'].map((p) => p[0].toUpperCase() + ': ' + d.alarms[p]).join('  ')));
    const siteCards = d.sites.map((s) => {
      const tot = Object.values(s.devices).reduce((a, b) => a + b, 0) || 1;
      const seg = (k, c) => h('i', { style: `width:${(s.devices[k] / tot) * 100}%;background:${c}` });
      return h('div', { class: 'card tile', onclick: () => (location.hash = '#/devices?site_id=' + s.id) }, h('div', { class: 'small mut' }, s.company_name),
        h('h2', { style: 'margin:0' }, s.name), h('div', { class: 'small' }, tot === 1 && !Object.values(s.devices).some(Boolean) ? 'No devices' : `${s.devices.online + s.devices.alarm} of ${Object.values(s.devices).reduce((a, b) => a + b, 0)} communicating`,
          s.active_alarms ? h('span', { class: 'pill p-critical', style: 'margin-left:8px' }, s.active_alarms + ' alarm(s)') : null),
        h('div', { class: 'bar' }, seg('online', 'var(--ok)'), seg('alarm', 'var(--bad)'), seg('comm_lost', 'var(--lost)'), seg('offline', 'var(--off)')));
    });
    clear(box).append(
      d.simulated_devices ? h('div', { class: 'banner' }, h('b', null, 'Simulated data: '), d.simulated_devices + ' device(s) on this platform generate test values. They are labelled SIMULATED everywhere. Real devices appear only after a TBox Edge Gateway connects to them.') : null,
      h('div', { class: 'grid g4' }, tiles, alarmCard,
        h('div', { class: 'card tile', onclick: () => (location.hash = '#/gateways') }, h('div', { class: 'n' }, d.gateways.online + ' / ' + d.gateways.total), h('div', { class: 'l' }, 'Gateways online'))),
      h('h3', { style: 'margin:18px 0 8px' }, 'Sites'), h('div', { class: 'grid g3' }, siteCards.length ? siteCards : h('div', { class: 'mut' }, 'No sites yet.')),
      h('h3', { style: 'margin:18px 0 8px' }, 'Latest alarms'),
      table([{ label: 'Priority', render: (a) => prioPill(a.priority) }, { label: 'Time', render: (a) => fmtTime(a.raised_ts) }, { label: 'Message', wrap: true, render: (a) => a.message },
        { label: 'State', render: (a) => (a.active ? 'Active' : 'Cleared') + (a.acked_ts ? ', acknowledged' : '') }], d.recent_alarms, 'No alarms recorded.'));
  };
  await draw();
  ctx.every(3000, draw);
}

// ------------------------------------------------------------------ live
export async function live(box, ctx) {
  const devices = await get('/api/devices');
  let tab = 'mine';
  const content = h('div');
  const tabs = h('div', { class: 'tabs' });
  const setTab = (t) => { tab = t; renderTabs(); load(); };
  const renderTabs = () => { clear(tabs).append(h('button', { class: tab === 'mine' ? 'on' : '', onclick: () => setTab('mine') }, 'My dashboard'), h('button', { class: tab === 'device' ? 'on' : '', onclick: () => setTab('device') }, 'By device')); };
  box.append(tabs, content);

  let timer;
  const stop = () => clearInterval(timer);
  ctx.onCleanup(stop);

  async function deviceView() {
    if (!devices.length) return content.append(h('div', { class: 'mut' }, 'No devices yet. Add one under Devices.'));
    const sel = select(devices.map((d) => [d.id, d.name + ' - ' + d.site_name]), ctx.params.device_id || devices[0].id);
    const grid = h('div', { class: 'grid g4' });
    const info = h('div', { class: 'row small' });
    clear(content).append(h('div', { class: 'row', style: 'margin-bottom:12px' }, sel), info, grid);
    const draw = async () => {
      const r = await get('/api/live?device_id=' + sel.value);
      const dv = r.devices[0];
      clear(info).append(dv ? statusPill(dv.status) : null, dv && dv.last_comm_ts ? h('span', { class: 'mut' }, 'last communication ' + ago(dv.last_comm_ts)) : null, dv && dv.last_error ? h('span', { class: 'pill q-bad' }, dv.last_error) : null);
      grid.replaceChildren(...r.tags.map((t) => tagCard(t)));
      if (!r.tags.length) grid.append(h('div', { class: 'mut' }, 'This device has no tags yet.'));
    };
    sel.onchange = () => draw().catch((e) => toast(e.message, 'err'));
    await draw(); timer = setInterval(() => draw().catch(() => {}), 2000);
  }

  async function myDashboard() {
    const layout = (await get('/api/dashboard/layout')).widgets;
    const grid = h('div', { class: 'grid g4' });
    const add = h('button', { class: 'btn pri', onclick: addWidget }, '+ Add widget');
    clear(content).append(h('div', { class: 'row', style: 'margin-bottom:12px' }, add, h('span', { class: 'small mut' }, 'Your personal layout is saved on the server and follows you to every device.')), grid);
    const save = async () => { try { await put('/api/dashboard/layout', { widgets: layout }); } catch (e) { toast(e.message, 'err'); } };
    let tags = {};
    const draw = async () => {
      if (!layout.length) { grid.replaceChildren(h('div', { class: 'mut' }, 'Empty. Press "Add widget" to pin gauges, values, status lamps and trends.')); return; }
      const r = await get('/api/live?tag_ids=' + layout.map((w) => w.tag_id).join(','));
      tags = Object.fromEntries(r.tags.map((t) => [t.id, t]));
      grid.replaceChildren(...layout.map((w, i) => {
        const t = tags[w.tag_id];
        if (!t) return h('div', { class: 'card wcard mut' }, 'Tag no longer available', h('button', { class: 'btn sm', onclick: () => { layout.splice(i, 1); save(); draw(); } }, 'Remove'));
        const ctl = h('div', { class: 'x' }, h('button', { class: 'btn sm', title: 'Move earlier', onclick: () => move(i, -1) }, '◀'), h('button', { class: 'btn sm', title: 'Move later', onclick: () => move(i, 1) }, '▶'),
          h('button', { class: 'btn sm', title: 'Remove', onclick: () => { layout.splice(i, 1); save(); draw(); } }, '✕'));
        return tagCard(t, w.type, ctl);
      }));
    };
    const move = (i, d) => { const j = i + d; if (j < 0 || j >= layout.length) return; [layout[i], layout[j]] = [layout[j], layout[i]]; save(); draw(); };
    async function addWidget() {
      const dsel = select(devices.map((d) => [d.id, d.name]), devices[0] && devices[0].id);
      const tsel = select([], '');
      const typ = select([['gauge', 'Gauge'], ['value', 'Big value'], ['status', 'Status lamp (ON/OFF)'], ['trend', 'Trend (last hour)']], 'gauge');
      const fill = async () => { const r = await get('/api/tags?device_id=' + dsel.value); tsel.replaceChildren(...r.filter((t) => t.enabled).map((t) => h('option', { value: t.id }, t.name + (t.unit ? ' (' + t.unit + ')' : '')))); };
      dsel.onchange = fill; await fill();
      modal('Add widget', h('div', null, field('Device', dsel), field('Tag', tsel), field('Widget type', typ)), { small: true, buttons: [
        { label: 'Cancel', onclick: () => {} },
        { label: 'Add', cls: 'pri', onclick: () => { if (!tsel.value) return false; layout.push({ type: typ.value, tag_id: Number(tsel.value) }); save(); draw(); } }] });
    }
    await draw(); timer = setInterval(() => draw().catch(() => {}), 2000);
  }
  async function load() { stop(); clear(content); try { await (tab === 'mine' ? myDashboard() : deviceView()); } catch (e) { content.append(h('div', { class: 'banner err' }, e.message)); } }
  renderTabs(); await load();
}

// ------------------------------------------------------------------ trends
export async function trends(box, ctx) {
  const devices = await get('/api/devices');
  if (!devices.length) return box.append(h('div', { class: 'mut' }, 'No devices yet.'));
  const picked = []; // {id,name,unit,device,simulated}
  const ranges = [['15 min', 900000], ['1 hour', 3600000], ['6 hours', 21600000], ['24 hours', 86400000], ['7 days', 604800000], ['30 days', 2592000000], ['1 year', 31536000000]];
  let span = 3600000, customFrom = null, customTo = null, compare = false;
  const dsel = select(devices.map((d) => [d.id, d.name]), ctx.params.device_id || devices[0].id);
  const tsel = select([], '');
  const chips = h('div', { class: 'chips' });
  const out = h('div', { class: 'stack' });
  const rangeBtns = h('div', { class: 'row' });
  const fromI = h('input', { type: 'datetime-local' }), toI = h('input', { type: 'datetime-local' });
  const fill = async () => { const r = await get('/api/tags?device_id=' + dsel.value); tsel.replaceChildren(...r.map((t) => h('option', { value: t.id, 'data-unit': t.unit, 'data-name': t.name }, t.name + (t.unit ? ' (' + t.unit + ')' : '')))); };
  dsel.onchange = fill; await fill();
  if (ctx.params.tag_id) { const t = (await get('/api/tags?device_id=' + dsel.value)).find((x) => String(x.id) === ctx.params.tag_id); if (t) picked.push(t); }
  const drawChips = () => clear(chips).append(...picked.map((t, i) => h('span', { class: 'chip' }, h('i', { style: `width:8px;height:8px;border-radius:2px;background:${color(i)}` }), t.device_name + ' / ' + t.name, h('b', { onclick: () => { picked.splice(i, 1); drawChips(); render(); } }, '✕'))));
  const addBtn = h('button', { class: 'btn', onclick: async () => {
    if (picked.length >= 6) return toast('Maximum 6 series', 'err');
    const t = (await get('/api/tags?device_id=' + dsel.value)).find((x) => String(x.id) === tsel.value);
    if (t && !picked.some((p) => p.id === t.id)) { picked.push(t); drawChips(); render(); } } }, 'Add to chart');
  const drawRange = () => clear(rangeBtns).append(...ranges.map(([l, ms]) => h('button', { class: 'btn sm' + (!customFrom && span === ms ? ' pri' : ''), onclick: () => { span = ms; customFrom = customTo = null; drawRange(); render(); } }, l)),
    h('span', { class: 'mut small' }, 'or'), fromI, toI, h('button', { class: 'btn sm', onclick: () => { if (!fromI.value || !toI.value) return; customFrom = new Date(fromI.value).getTime(); customTo = new Date(toI.value).getTime(); drawRange(); render(); } }, 'Apply'));
  const cmp = h('input', { type: 'checkbox', onchange: (e) => { compare = e.target.checked; render(); } });
  const range = () => (customFrom ? [customFrom, customTo] : [Date.now() - span, Date.now()]);

  async function render() {
    if (!picked.length) return clear(out).append(h('div', { class: 'mut' }, 'Pick one or more tags to plot. Each tag gets its own chart so different units stay readable; the pointer line is synchronised in the tooltip.'));
    const [from, to] = range();
    const cards = [];
    for (let i = 0; i < picked.length; i++) {
      const t = picked[i];
      try {
        const r = await get('/api/tags/' + t.id + '/history?' + qs({ from, to, points: 700 }));
        const series = [{ name: t.name, color: color(i), unit: t.unit, points: r.points, bucket: r.bucket_ms }];
        if (compare) {
          const w = to - from, rp = await get('/api/tags/' + t.id + '/history?' + qs({ from: from - w, to: from, points: 700 }));
          series.push({ name: t.name + ' (previous period)', color: '#8b9ab0', unit: t.unit, dashed: true, bucket: rp.bucket_ms, points: rp.points.map((p) => [p[0] + w, ...p.slice(1)]) });
        }
        const nBad = r.points.reduce((a, p) => a + (p[5] || 0), 0);
        cards.push(h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { style: 'margin:0' }, t.device_name + ' / ' + t.name + (t.unit ? ' (' + t.unit + ')' : '')), r.tag.simulated ? simBadge() : null, nBad ? h('span', { class: 'pill q-stale' }, nBad + ' non-good sample(s) in range') : null),
          h('div', { class: 'legend' }, series.map((s) => h('span', null, h('i', { style: 'background:' + s.color }), s.name)), h('span', { class: 'mut' }, 'shaded band = min..max inside each interval')),
          lineChart(series, { from, to, limits: { lolo: t.lolo, lo: t.lo, hi: t.hi, hihi: t.hihi } })));
      } catch (e) { cards.push(h('div', { class: 'banner err' }, t.name + ': ' + e.message)); }
    }
    out.replaceChildren(...cards);
  }
  const exp = (fmt) => () => { if (!picked.length) return toast('Pick tags first', 'err'); const [from, to] = range();
    download('/api/reports/export?' + qs({ kind: 'history', format: fmt, tag_ids: picked.map((p) => p.id).join(','), from, to, tz: -new Date().getTimezoneOffset() }), 'history.' + fmt).catch((e) => toast(e.message, 'err')); };
  box.append(h('div', { class: 'card stack', style: 'margin-bottom:14px' }, h('div', { class: 'row' }, dsel, tsel, addBtn), chips, rangeBtns,
    h('div', { class: 'row' }, h('label', { style: 'margin:0;display:flex;gap:6px;align-items:center' }, cmp, 'Compare with previous period'), h('span', { class: 'sp' }),
      h('button', { class: 'btn sm', onclick: exp('csv') }, 'CSV'), h('button', { class: 'btn sm', onclick: exp('xlsx') }, 'Excel'), h('button', { class: 'btn sm', onclick: exp('pdf') }, 'PDF'))), out);
  drawRange(); drawChips(); await render();
  ctx.every(30000, async () => { if (!customFrom) await render(); });
}

// ------------------------------------------------------------------ alarms
export async function alarms(box, ctx) {
  let state = 'active', prio = '';
  const body = h('div');
  const pSel = select([['', 'All priorities'], ['critical', 'Critical'], ['high', 'High'], ['medium', 'Medium'], ['low', 'Low']], '', { onchange: (e) => { prio = e.target.value; draw(); } });
  const tabs = h('div', { class: 'tabs' });
  const setState = (s) => { state = s; drawTabs(); draw(); };
  const drawTabs = () => clear(tabs).append(h('button', { class: state === 'active' ? 'on' : '', onclick: () => setState('active') }, 'Active & unacknowledged'), h('button', { class: state === 'history' ? 'on' : '', onclick: () => setState('history') }, 'History'));
  async function draw() {
    const rows = await get('/api/alarms?' + qs({ state, priority: prio, limit: 300 }));
    const ack = async (a) => { try { await post('/api/alarms/' + a.id + '/ack'); draw(); } catch (e) { toast(e.message, 'err'); } };
    clear(body).append(table([
      { label: 'Priority', render: (a) => prioPill(a.priority) }, { label: 'Raised', render: (a) => fmtTime(a.raised_ts) },
      { label: 'Site / device', render: (a) => [a.site_name, ' / ', a.device_name, a.simulated ? [' ', simBadge()] : null] },
      { label: 'Alarm', wrap: true, render: (a) => a.message },
      { label: 'State', render: (a) => a.active ? h('span', { class: 'pill s-alarm' }, 'Active') : h('span', { class: 'mut' }, 'Cleared ' + fmtTime(a.cleared_ts)) },
      { label: 'Acknowledged', render: (a) => a.acked_ts ? a.acked_by + ' · ' + fmtTime(a.acked_ts) : h('button', { class: 'btn sm pri', onclick: () => ack(a) }, 'Acknowledge') }], rows, 'No alarms. All clear.'));
  }
  box.append(h('div', { class: 'row', style: 'margin-bottom:8px' }, tabs, h('span', { class: 'sp' }), pSel,
    h('button', { class: 'btn', onclick: async () => { if (await confirmBox('Acknowledge all', 'Acknowledge every open alarm you can see?', 'Acknowledge all')) { const r = await post('/api/alarms/ack-all'); toast(r.acknowledged + ' alarm(s) acknowledged', 'ok'); draw(); } } }, 'Acknowledge all'),
    h('button', { class: 'btn', onclick: () => download('/api/reports/export?' + qs({ kind: 'alarms', format: 'xlsx', from: Date.now() - 30 * 86400000, to: Date.now() }), 'alarms.xlsx').catch((e) => toast(e.message, 'err')) }, 'Export 30 days (Excel)')), body);
  drawTabs(); await draw(); ctx.every(3000, draw);
}

// ------------------------------------------------------------------ events
export async function events(box, ctx) {
  let cat = '', rows = [];
  const body = h('div');
  const more = h('button', { class: 'btn', onclick: async () => { const r = await get('/api/events?' + qs({ category: cat, limit: 100, before_id: rows[rows.length - 1].id })); rows = rows.concat(r); draw(); } }, 'Load older');
  const sel = select([['', 'All categories'], ['alarm', 'Alarms'], ['device', 'Devices'], ['system', 'System'], ['audit', 'Configuration & audit'], ['security', 'Security (admin)']], '', { onchange: async (e) => { cat = e.target.value; await reload(); } });
  const draw = () => clear(body).append(table([{ label: 'Time', render: (e) => fmtTime(e.ts) }, { label: 'Category', render: (e) => e.category },
    { label: 'Severity', render: (e) => h('span', { class: 'pill ' + (e.severity === 'error' ? 's-alarm' : e.severity === 'warning' ? 'q-stale' : 's-offline') }, e.severity) },
    { label: 'Event', wrap: true, render: (e) => e.message }, { label: 'User', render: (e) => e.username || '' }], rows, 'No events.'), rows.length >= 100 ? more : null);
  const reload = async () => { rows = await get('/api/events?' + qs({ category: cat, limit: 100 })); draw(); };
  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' }, sel), body);
  await reload(); ctx.every(5000, async () => { if (rows.length <= 100) await reload(); });
}
