import { get, post, put, del, download, qs } from './api.js';
import { h, clear, toast, modal, field, select, statusPill, simBadge, fmtNum, fmtTime, ago, table, confirmBox } from './ui.js';

const inp = (v, a = {}) => h('input', { value: v ?? '', ...a });
const pad = (n) => String(n).padStart(2, '0');
const localIso = (ms) => { const d = new Date(ms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const tz = () => -new Date().getTimezoneOffset();

// ------------------------------------------------------------------ reports
export async function reports(box, ctx) {
  const [sites, devs] = await Promise.all([get('/api/sites'), get('/api/devices')]);
  const kindSel = select([['consumption', 'Energy & fuel consumption / production'], ['summary', 'Single tag statistics (min / avg / max)'], ['alarms', 'Alarm history']], 'consumption');
  const periodSel = select([['hour', 'Hourly'], ['day', 'Daily'], ['month', 'Monthly'], ['year', 'Yearly']], 'day');
  const siteSel = select([['', 'All sites'], ...sites.map((s) => [s.id, s.name])], '');
  const dsel = select(devs.map((d) => [d.id, d.name]), devs[0]?.id), tsel = select([], '');
  const now = Date.now();
  const from = h('input', { type: 'datetime-local', value: localIso(now - 30 * 86400000) }), to = h('input', { type: 'datetime-local', value: localIso(now) });
  const out = h('div');
  const tagBox = h('div', { class: 'hide' });
  const fill = async () => { if (!dsel.value) return; const r = await get('/api/tags?device_id=' + dsel.value); tsel.replaceChildren(...r.map((t) => h('option', { value: t.id }, t.name + (t.unit ? ' (' + t.unit + ')' : '')))); };
  dsel.onchange = fill; await fill();
  const quick = (label, fn) => h('button', { class: 'btn sm', onclick: () => { const [a, b] = fn(); from.value = localIso(a); to.value = localIso(b); } }, label);
  const params = () => ({ from: new Date(from.value).getTime(), to: new Date(to.value).getTime(), period: periodSel.value, tz: tz(), site_id: siteSel.value, tag_id: tsel.value });
  const sync = () => tagBox.classList.toggle('hide', kindSel.value !== 'summary');
  kindSel.onchange = sync;

  const run = async () => {
    const p = params(); const k = kindSel.value;
    try {
      if (k === 'consumption') {
        const r = await get('/api/reports/consumption?' + qs(p));
        clear(out).append(h('p', { class: 'small mut' }, 'Counters (energy, fuel used, hours): sum of positive increments. Fuel level tanks: sum of drops; refuelling jumps are ignored. A counter reset is never counted as negative use. Only Good-quality readings are used.'),
          table([{ label: 'Period', render: (x) => x.period }, { label: 'Device', render: (x) => [x.device, x.simulated ? [' ', simBadge()] : null] }, { label: 'Tag', render: (x) => x.tag }, { label: 'Calculation', render: (x) => x.type },
            { label: 'Consumption / production', render: (x) => fmtNum(x.consumption, 2) + ' ' + x.unit }], r.rows, 'No counter or fuel-level tags have data in this range. Mark tags as "Cumulative counter" or "Level" in the Tag Manager.'));
      } else if (k === 'summary') {
        const r = await get('/api/reports/summary?' + qs(p));
        clear(out).append(h('div', { class: 'row' }, h('h2', null, r.tag.device + ' / ' + r.tag.name + ' (' + r.tag.unit + ')'), r.tag.simulated ? simBadge() : null),
          table([{ label: 'Period', render: (x) => x.period }, { label: 'Samples', render: (x) => x.samples }, { label: 'Min', render: (x) => fmtNum(x.min) }, { label: 'Average', render: (x) => fmtNum(x.avg) }, { label: 'Max', render: (x) => fmtNum(x.max) },
            { label: 'Consumption', render: (x) => (x.consumption == null ? '—' : fmtNum(x.consumption, 2)) }], r.rows, 'No data in this range.'));
      } else {
        const rows = await get('/api/alarms?state=history&limit=500');
        clear(out).append(table([{ label: 'Raised', render: (a) => fmtTime(a.raised_ts) }, { label: 'Priority', render: (a) => a.priority }, { label: 'Device', render: (a) => a.device_name }, { label: 'Alarm', wrap: true, render: (a) => a.message }, { label: 'Acknowledged by', render: (a) => a.acked_by || '' }], rows));
      }
    } catch (e) { toast(e.message, 'err'); }
  };
  const exp = (fmt) => () => { const p = params(); const k = kindSel.value;
    download('/api/reports/export?' + qs({ ...p, kind: k, format: fmt }), 'report.' + fmt).catch((e) => toast(e.message, 'err')); };
  box.append(h('div', { class: 'card stack', style: 'margin-bottom:14px' }, h('div', { class: 'form-grid' }, field('Report', kindSel), field('Period', periodSel), field('Site', siteSel), field('From', from), field('To', to)),
    tagBox.appendChild(h('div', { class: 'form-grid' }, field('Device', dsel), field('Tag', tsel))) && tagBox,
    h('div', { class: 'row' }, quick('Today', () => [new Date().setHours(0, 0, 0, 0), Date.now()]), quick('Last 7 days', () => [Date.now() - 7 * 86400000, Date.now()]), quick('Last 30 days', () => [Date.now() - 30 * 86400000, Date.now()]),
      quick('This month', () => [new Date(new Date().getFullYear(), new Date().getMonth(), 1).getTime(), Date.now()]), quick('This year', () => [new Date(new Date().getFullYear(), 0, 1).getTime(), Date.now()]),
      h('span', { class: 'sp' }), h('button', { class: 'btn pri', onclick: run }, 'Run report'), h('button', { class: 'btn', onclick: exp('xlsx') }, 'Excel'), h('button', { class: 'btn', onclick: exp('pdf') }, 'PDF'), h('button', { class: 'btn', onclick: exp('csv') }, 'CSV'))), out);
  sync(); await run();
}

// ------------------------------------------------------------------ gateways
export async function gateways(box, ctx) {
  const sites = await get('/api/sites');
  const body = h('div');
  const tokenBox = (gw, token) => modal('Gateway token: ' + gw, h('div', null,
    h('div', { class: 'banner' }, 'Copy this token now. It is shown only once and cannot be recovered; rotate the token if you lose it.'),
    h('textarea', { rows: 2, readonly: true, onfocus: (e) => e.target.select() }, token),
    h('p', { class: 'small mut' }, 'On the gateway computer, put it in gateway.json (see gateway/gateway.example.json) together with this server\'s address. The gateway connects OUTWARD to TBox; nothing on the plant network needs to be exposed to the internet.')), { buttons: [{ label: 'Done', cls: 'pri', onclick: () => {} }] });
  async function draw() {
    const rows = await get('/api/gateways');
    clear(body).append(table([
      { label: 'Status', render: (g) => statusPill(g.online ? 'online' : 'offline') }, { label: 'Gateway', render: (g) => g.name }, { label: 'Site', render: (g) => sites.find((s) => s.id === g.site_id)?.name },
      { label: 'Devices', render: (g) => g.device_count }, { label: 'Last seen', render: (g) => ago(g.last_seen) }, { label: 'Version', render: (g) => g.version || '—' },
      { label: 'Drivers', render: (g) => (g.protocols || []).join(', ') || '—' }, { label: 'Buffered', render: (g) => (g.buffer_depth ? g.buffer_depth + ' records' : '0') },
      { label: 'Clock offset', render: (g) => (g.clock_skew_ms == null ? '—' : Math.abs(g.clock_skew_ms) > 5000 ? h('span', { class: 'pill q-stale' }, g.clock_skew_ms + ' ms') : g.clock_skew_ms + ' ms') },
      { label: '', render: (g) => ctx.isAdmin ? h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: async () => { if (await confirmBox('Rotate token', 'The current token stops working immediately. Continue?', 'Rotate')) { const r = await post('/api/gateways/' + g.id + '/rotate-token'); tokenBox(g.name, r.token); } } }, 'Rotate token'),
        h('button', { class: 'btn sm dng', onclick: async () => { if (await confirmBox('Delete gateway', 'Devices assigned to it will stop receiving data.', 'Delete', true)) { await del('/api/gateways/' + g.id); draw(); } } }, 'Delete')) : null }], rows, 'No gateways. Create one, then install the TBox Edge Gateway on a computer at the site.'));
  }
  const add = () => {
    const name = inp(''), desc = inp(''), site = select(sites.map((s) => [s.id, s.name]), sites[0]?.id);
    modal('Add gateway', h('div', null, field('Name', name, 'e.g. Plant-room PC'), field('Site', site), field('Description', desc)), { small: true, buttons: [{ label: 'Cancel', onclick: () => {} },
      { label: 'Create', cls: 'pri', onclick: async () => { try { const r = await post('/api/gateways', { name: name.value, site_id: Number(site.value), description: desc.value }); tokenBox(name.value, r.token); draw(); } catch (e) { toast(e.message, 'err'); return false; } } }] });
  };
  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' }, ctx.isAdmin ? h('button', { class: 'btn pri', onclick: add }, '+ Add gateway') : null), body);
  await draw(); ctx.every(4000, draw);
}

// ------------------------------------------------------------------ sites & structure
export async function sites(box, ctx) {
  const body = h('div');
  async function draw() {
    const [tree, companies] = await Promise.all([get('/api/tree'), get('/api/companies')]);
    const byCo = {};
    tree.forEach((s) => (byCo[s.company_name] = byCo[s.company_name] || []).push(s));
    clear(body).append(h('div', { class: 'tree' }, h('ul', { style: 'margin-left:0;border:0;padding:0' }, Object.entries(byCo).map(([co, ss]) => h('li', null, h('b', null, '🏢 ' + co),
      h('ul', null, ss.map((s) => h('li', null, h('b', null, '⌂ ' + s.name), ' ', h('span', { class: 'mut small' }, s.address || ''),
        h('button', { class: 'btn sm', style: 'margin-left:8px', onclick: () => addBuilding(s) }, '+ building'), h('button', { class: 'btn sm', onclick: () => editSite(s) }, 'Edit'), h('button', { class: 'btn sm dng', onclick: () => delSite(s) }, 'Delete'),
        h('ul', null, s.buildings.map((b) => h('li', null, '▭ ' + b.name + ' ', h('button', { class: 'btn sm dng', onclick: async () => { if (await confirmBox('Delete building', 'Devices stay on the site but lose this building.', 'Delete', true)) { await del('/api/buildings/' + b.id); draw(); } } }, '✕'))),
          s.gateways.map((g) => h('li', { class: 'mut' }, '⇄ gateway: ' + g.name)),
          s.devices.map((d) => h('li', null, '▣ ', h('a', { href: '#/live?device_id=' + d.id }, d.name), ' ', statusPill(d.status), d.source === 'simulated' ? [' ', simBadge()] : null)))))))))),
      tree.length ? null : h('div', { class: 'mut' }, 'No sites yet.'));
    body.companies = companies;
  }
  const addCompany = () => { const n = inp(''); modal('Add company', field('Company name', n), { small: true, buttons: [{ label: 'Cancel', onclick: () => {} }, { label: 'Create', cls: 'pri', onclick: async () => { try { await post('/api/companies', { name: n.value }); draw(); } catch (e) { toast(e.message, 'err'); return false; } } }] }); };
  const siteForm = (s) => {
    const cos = body.companies || [];
    const co = select(cos.map((c) => [c.id, c.name]), s?.company_id || cos[0]?.id, { disabled: !!s });
    const name = inp(s?.name), addr = inp(s?.address), desc = inp(s?.description), tzI = inp(s?.timezone || 'UTC');
    modal(s ? 'Edit site' : 'Add site', h('div', null, field('Company', co), field('Site name', name), field('Address / location', addr), field('Description', desc), field('Time zone label', tzI)), { small: true, buttons: [{ label: 'Cancel', onclick: () => {} },
      { label: 'Save', cls: 'pri', onclick: async () => { const p = { company_id: Number(co.value), name: name.value, address: addr.value, description: desc.value, timezone: tzI.value };
        try { s ? await put('/api/sites/' + s.id, p) : await post('/api/sites', p); draw(); } catch (e) { toast(e.message, 'err'); return false; } } }] });
  };
  const editSite = (s) => siteForm(s);
  const delSite = async (s) => { if (await confirmBox('Delete site', 'Delete "' + s.name + '"? The site must have no devices.', 'Delete', true)) { try { await del('/api/sites/' + s.id); draw(); } catch (e) { toast(e.message, 'err'); } } };
  const addBuilding = (s) => { const n = inp(''); modal('Add building to ' + s.name, field('Building / facility name', n), { small: true, buttons: [{ label: 'Cancel', onclick: () => {} }, { label: 'Create', cls: 'pri', onclick: async () => { try { await post('/api/buildings', { site_id: s.id, name: n.value }); draw(); } catch (e) { toast(e.message, 'err'); return false; } } }] }); };
  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' }, ctx.user.company_id == null ? h('button', { class: 'btn', onclick: addCompany }, '+ Company') : null, h('button', { class: 'btn pri', onclick: () => siteForm(null) }, '+ Site')), h('div', { class: 'card' }, body));
  await draw();
}

// ------------------------------------------------------------------ users
export async function users(box, ctx) {
  const [sites] = await Promise.all([get('/api/sites')]);
  const body = h('div');
  async function draw() {
    const rows = await get('/api/users');
    clear(body).append(table([{ label: 'User', render: (u) => [h('b', null, u.username), h('div', { class: 'small mut' }, u.full_name)] }, { label: 'Role', render: (u) => u.role },
      { label: 'Sites', render: (u) => (u.role === 'admin' ? 'all in company' : u.site_ids.map((id) => sites.find((s) => s.id === id)?.name).filter(Boolean).join(', ') || 'none') },
      { label: 'Status', render: (u) => (u.active ? 'active' : h('span', { class: 'pill s-offline' }, 'disabled')) }, { label: 'Last login', render: (u) => fmtTime(u.last_login) },
      { label: '', render: (u) => h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: () => form(u) }, 'Edit'), u.id === ctx.user.id ? null : h('button', { class: 'btn sm dng', onclick: async () => { if (await confirmBox('Delete user', 'Delete ' + u.username + '?', 'Delete', true)) { try { await del('/api/users/' + u.id); draw(); } catch (e) { toast(e.message, 'err'); } } } }, 'Delete')) }], rows));
  }
  function form(u) {
    const un = inp(u?.username, { disabled: !!u }), fn = inp(u?.full_name), em = inp(u?.email), pw = h('input', { type: 'password', autocomplete: 'new-password', placeholder: u ? '(leave empty to keep)' : '' });
    const role = select([['operator', 'Operator - view and acknowledge alarms'], ['engineer', 'Engineer - configure devices and tags'], ['admin', 'Administrator - everything']], u?.role || 'operator');
    const active = h('input', { type: 'checkbox', checked: u ? u.active : true });
    const checks = sites.map((s) => [s, h('input', { type: 'checkbox', checked: u ? u.site_ids.includes(s.id) : false })]);
    modal(u ? 'Edit user' : 'Add user', h('div', null, h('div', { class: 'form-grid' }, field('Username', un), field('Full name', fn), field('E-mail', em), field(u ? 'New password' : 'Initial password', pw, 'The user must change it at first sign-in.')), field('Role', role),
      h('h3', null, 'Sites this user may access (not needed for administrators)'), h('div', null, checks.map(([s, c]) => h('label', { style: 'display:flex;gap:8px;align-items:center;color:var(--text)' }, c, s.company_name + ' / ' + s.name))),
      u ? h('label', { style: 'display:flex;gap:8px;align-items:center' }, active, 'Account active') : null), { buttons: [{ label: 'Cancel', onclick: () => {} }, { label: 'Save', cls: 'pri', onclick: async () => {
        const p = { username: un.value, full_name: fn.value, email: em.value, password: pw.value, role: role.value, active: active.checked, site_ids: checks.filter(([, c]) => c.checked).map(([s]) => s.id) };
        try { u ? await put('/api/users/' + u.id, p) : await post('/api/users', p); draw(); } catch (e) { toast(e.message, 'err'); return false; } } }] });
  }
  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' }, h('button', { class: 'btn pri', onclick: () => form(null) }, '+ Add user')), body);
  await draw();
}

// ------------------------------------------------------------------ settings, backups, notifications
export async function settings(box, ctx) {
  const [sys, st, sites] = await Promise.all([get('/api/system'), get('/api/settings'), get('/api/sites')]);
  const f = { retention_samples_days: inp(st.retention_samples_days, { type: 'number' }), retention_events_days: inp(st.retention_events_days, { type: 'number' }), retention_alarms_days: inp(st.retention_alarms_days, { type: 'number' }),
    backup_keep: inp(st.backup_keep, { type: 'number' }), gateway_offline_after_s: inp(st.gateway_offline_after_s, { type: 'number' }) };
  const be = h('input', { type: 'checkbox', checked: st.backup_enabled });
  const ce = h('input', { type: 'checkbox', checked: st.control_enabled });
  const backups = h('div'), notif = h('div');
  const drawBackups = async () => { const r = await get('/api/backups'); clear(backups).append(table([{ label: 'Backup file', render: (b) => b.name }, { label: 'Size', render: (b) => (b.size / 1048576).toFixed(2) + ' MB' }, { label: 'Created', render: (b) => fmtTime(b.modified) }, { label: '', render: (b) => h('button', { class: 'btn sm', onclick: () => download('/api/backups/' + b.name + '/download', b.name).catch((e) => toast(e.message, 'err')) }, 'Download') }], r, 'No backups yet.')); };
  const drawNotif = async () => { const r = await get('/api/notifications'); clear(notif).append(table([{ label: 'Channel', render: (c) => c.name }, { label: 'Type', render: (c) => c.type }, { label: 'Destination', render: (c) => (c.type === 'webhook' ? c.config.url : c.config.to.join(', ')) }, { label: 'From priority', render: (c) => c.min_priority },
    { label: 'Site', render: (c) => sites.find((s) => s.id === c.site_id)?.name || 'all' }, { label: '', render: (c) => h('button', { class: 'btn sm dng', onclick: async () => { await del('/api/notifications/' + c.id); drawNotif(); } }, 'Delete') }], r, 'No notification channels.')); };
  const addNotif = () => {
    const typ = select([['webhook', 'Webhook (HTTP POST JSON)'], ['email', 'E-mail (SMTP)']], 'webhook'), name = inp(''), url = inp('', { placeholder: 'https://…' });
    const host = inp(''), port = inp('587'), user = inp(''), pass = h('input', { type: 'password' }), from = inp(''), to = inp('', { placeholder: 'a@x.com, b@y.com' });
    const prio = select([['low', 'Low and above'], ['medium', 'Medium and above'], ['high', 'High and above'], ['critical', 'Critical only']], 'high'), site = select([['', 'All sites'], ...sites.map((s) => [s.id, s.name])], '');
    const wb = h('div', null, field('Webhook URL', url)), eb = h('div', { class: 'form-grid hide' }, field('SMTP host', host), field('Port', port), field('SMTP user', user), field('SMTP password', pass), field('From address', from), field('Recipients', to));
    typ.onchange = () => { wb.classList.toggle('hide', typ.value !== 'webhook'); eb.classList.toggle('hide', typ.value !== 'email'); };
    modal('Add notification channel', h('div', null, field('Name', name), field('Type', typ), wb, eb, field('Send alarms of priority', prio), field('Site', site)), { buttons: [{ label: 'Cancel', onclick: () => {} }, { label: 'Add', cls: 'pri', onclick: async () => {
      const config = typ.value === 'webhook' ? { url: url.value } : { host: host.value, port: Number(port.value), username: user.value, password: pass.value, from: from.value, to: to.value };
      try { await post('/api/notifications', { name: name.value, type: typ.value, config, min_priority: prio.value, site_id: site.value ? Number(site.value) : null }); drawNotif(); } catch (e) { toast(e.message, 'err'); return false; } } }] });
  };
  const saveSettings = async () => { const p = { backup_enabled: be.checked, control_enabled: ce.checked }; for (const [k, el] of Object.entries(f)) p[k] = el.value; try { await put('/api/settings', p); toast('Settings saved', 'ok'); } catch (e) { toast(e.message, 'err'); } };
  const kv = (pairs) => h('div', { class: 'kv' }, pairs.flatMap(([k, v]) => [h('div', null, k), h('div', null, String(v))]));
  box.append(h('div', { class: 'stack' },
    sys.warnings.map((w) => h('div', { class: 'banner err' }, w)),
    h('div', { class: 'card' }, h('h2', null, 'System'), kv([['TBox version', sys.version], ['Python', sys.python], ['Uptime', Math.round(sys.uptime_s / 60) + ' min'], ['Database size', (sys.db_size_bytes / 1048576).toFixed(1) + ' MB'], ['Stored samples', sys.samples.toLocaleString()],
      ['Devices / tags', sys.devices + ' / ' + sys.tags], ['Gateways online', sys.gateways_online + ' of ' + sys.gateways_total], ['Credential encryption', sys.credential_encryption ? 'on' : 'OFF (install cryptography)'], ['Built-in simulator', sys.simulator_enabled ? 'running (simulated devices produce data)' : 'off'], ['Remote control', sys.remote_control_enabled ? 'ENABLED' : 'off (read-only)']])),
    h('div', { class: 'card' }, h('h2', null, 'Data retention, backups and gateway health'), h('div', { class: 'form-grid' }, field('Keep measurements (days)', f.retention_samples_days), field('Keep events (days)', f.retention_events_days), field('Keep alarm history (days)', f.retention_alarms_days),
      field('Backups to keep', f.backup_keep), field('Gateway offline after (s)', f.gateway_offline_after_s)), h('label', { style: 'display:flex;gap:8px;align-items:center' }, be, 'Automatic daily backup'),
      h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn pri', onclick: saveSettings }, 'Save settings'))),
    h('div', { class: 'card' }, h('h2', null, 'Remote control (writing to equipment)'), h('p', { class: 'small mut' }, 'Master switch. A write also needs: a tag set to Read/Write, an Administrator or Engineer account, an explicit confirmation, a gateway with allow_writes=true in its own local config, and the PLC accepting the value. Writes are audited and verified by read-back. Never rely on TBox for safety interlocks.'),
      h('label', { style: 'display:flex;gap:8px;align-items:center' }, ce, 'Enable remote control'), h('div', { class: 'row', style: 'margin-top:10px' }, h('button', { class: 'btn pri', onclick: saveSettings }, 'Save settings'))),
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { style: 'margin:0' }, 'Database backups'), h('span', { class: 'sp' }), h('button', { class: 'btn', onclick: async () => { try { await post('/api/backups'); toast('Backup created', 'ok'); drawBackups(); } catch (e) { toast(e.message, 'err'); } } }, 'Back up now')),
      h('p', { class: 'small mut' }, 'Backups are verified copies of the whole database in the server\'s data/backups folder. Copy them off the server regularly. To recover, stop the server and run: python tools/restore_backup.py <file>.'), backups),
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { style: 'margin:0' }, 'Alarm notifications'), h('span', { class: 'sp' }), h('button', { class: 'btn pri', onclick: addNotif }, '+ Add channel')), notif)));
  await Promise.all([drawBackups(), drawNotif()]);
}
