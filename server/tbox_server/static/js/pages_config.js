import { api, get, post, put, del, download, qs } from './api.js';
import { canWrite, openWrite } from './control.js';
import { h, clear, toast, modal, field, select, statusPill, qualityPill, simBadge, fmtNum, fmtTime, ago, table, confirmBox } from './ui.js';

const CATEGORIES = [['plc', 'PLC'], ['energy_meter', 'Energy meter'], ['generator', 'Generator / controller'], ['solar_inverter', 'Solar inverter'], ['vfd', 'Variable-frequency drive'],
  ['pump', 'Pump'], ['motor', 'Motor'], ['sensor', 'Sensor'], ['controller', 'Industrial controller'], ['gateway_device', 'Gateway device'], ['other', 'Other']];
const PROTOCOLS = [['modbus_tcp', 'Modbus TCP (Ethernet)'], ['modbus_rtu', 'Modbus RTU (RS-485 serial)'], ['mqtt', 'MQTT (driver not yet available)'], ['opcua', 'OPC UA (driver not yet available)']];
const DATA_TYPES = ['bool', 'int16', 'uint16', 'int32', 'uint32', 'float32', 'int64', 'uint64', 'float64'];
const ORDERS = [['ABCD', 'ABCD - big-endian'], ['CDAB', 'CDAB - word-swapped'], ['BADC', 'BADC - byte-swapped'], ['DCBA', 'DCBA - little-endian']];

const inp = (v, a = {}) => h('input', { value: v ?? '', ...a });
const num = (v, a = {}) => h('input', { type: 'number', step: 'any', value: v ?? '', ...a });

// ------------------------------------------------------------------ devices
export async function devices(box, ctx) {
  const [sites, gateways, profiles] = await Promise.all([get('/api/sites'), get('/api/gateways'), get('/api/profiles')]);
  const filter = { site_id: ctx.params.site_id || '', status: ctx.params.status || '', q: '' };
  const body = h('div');
  const siteSel = select([['', 'All sites'], ...sites.map((s) => [s.id, s.name])], filter.site_id, { onchange: (e) => { filter.site_id = e.target.value; draw(); } });
  const statSel = select([['', 'Any status'], ['online', 'Online'], ['alarm', 'Alarm'], ['comm_lost', 'Communication lost'], ['offline', 'Offline']], filter.status, { onchange: (e) => { filter.status = e.target.value; draw(); } });
  const search = h('input', { placeholder: 'Search…', style: 'max-width:200px', oninput: (e) => { filter.q = e.target.value; draw(); } });

  async function draw() {
    const rows = await get('/api/devices?' + qs(filter));
    const act = (label, fn, cls = '') => h('button', { class: 'btn sm ' + cls, onclick: fn }, label);
    clear(body).append(table([
      { label: 'Status', render: (d) => statusPill(d.status) },
      { label: 'Device', render: (d) => [h('a', { href: '#/live?device_id=' + d.id }, d.name), ' ', d.simulated ? simBadge() : null, d.enabled ? null : h('span', { class: 'pill s-offline', style: 'margin-left:6px' }, 'disconnected')] },
      { label: 'Site / building', render: (d) => d.site_name + (d.building_name ? ' / ' + d.building_name : '') },
      { label: 'Equipment', render: (d) => [d.manufacturer, d.model].filter(Boolean).join(' ') || d.category },
      { label: 'Protocol', render: (d) => (d.simulated ? 'simulated' : d.protocol.replace('_', ' ')) },
      { label: 'Gateway', render: (d) => d.gateway_name || '—' },
      { label: 'Last comm', render: (d) => ago(d.last_comm_ts) },
      { label: 'Tags', render: (d) => d.tag_count },
      { label: '', render: (d) => ctx.canEdit ? h('div', { class: 'row' },
        act('Diagnostics', () => diagnostics(d.id, draw)), act('Edit', () => editDevice(d.id)), act('Test', () => testDevice(d)), act('Duplicate', () => duplicate(d)),
        d.enabled ? act('Disconnect', () => disconnect(d)) : act('Connect', () => connect(d), 'pri'), act('Reconnect', () => reconnect(d)), act('Remove', () => remove(d), 'dng')) : act('Diagnostics', () => diagnostics(d.id, draw)) }],
      rows, 'No devices match. Use "Add device" to create one.'));
  }
  const connect = async (d) => { await post('/api/devices/' + d.id + '/connect'); toast('Connecting…', 'ok'); draw(); };
  const disconnect = async (d) => { if (await confirmBox('Disconnect device', 'Stop polling "' + d.name + '"? History is kept.', 'Disconnect')) { await post('/api/devices/' + d.id + '/disconnect'); draw(); } };
  const duplicate = async (d) => { try { const r = await post('/api/devices/' + d.id + '/duplicate', {}); toast(r.note, 'ok'); draw(); } catch (e) { toast(e.message, 'err'); } };
  const reconnect = async (d) => runCommand(d, 'reconnect');
  const testDevice = async (d) => runCommand(d, 'test');
  const remove = async (d) => {
    if (!(await confirmBox('Remove device', '"' + d.name + '" will be removed. If it has history it is archived (history kept) and can no longer be polled.', 'Remove', true))) return;
    try { const r = await del('/api/devices/' + d.id); toast(r.archived ? 'Device archived, history kept' : 'Device deleted', 'ok'); draw(); } catch (e) { toast(e.message, 'err'); }
  };
  async function runCommand(d, cmd) {
    try {
      const r = await post('/api/devices/' + d.id + '/' + cmd);
      if (r.done) return showResult(d, cmd, r.ok, r.message);
      toast('Request sent to the gateway…');
      for (let i = 0; i < 25; i++) {
        await new Promise((res) => setTimeout(res, 1000));
        const c = await get('/api/commands/' + r.command_id);
        if (c.done) return showResult(d, cmd, c.result && c.result.ok, c.result ? c.result.message : 'Request ' + c.status);
      }
      showResult(d, cmd, false, 'No answer from the gateway within 25 seconds.');
    } catch (e) { toast(e.message, 'err'); }
  }
  const showResult = (d, cmd, ok, msg) => modal((cmd === 'test' ? 'Connection test: ' : 'Reconnect: ') + d.name, h('div', { class: 'banner' + (ok ? '' : ' err') }, (ok ? '✔ ' : '✖ ') + msg), { small: true, buttons: [{ label: 'Close', onclick: () => {} }] });

  const editDevice = (id) => deviceForm(id ? { id } : null);

  async function deviceForm(existing) {
    let dev = {};
    if (existing) dev = await get('/api/devices/' + existing.id);
    const conn = dev.connection || {};
    const buildings = await get('/api/buildings');
    const siteSel2 = select(sites.map((s) => [s.id, s.name]), dev.site_id || sites[0]?.id, { disabled: !!existing });
    const bSel = select([], '');
    const gwSel = select([], '');
    const src = select([['gateway', 'Real device via TBox Edge Gateway'], ['simulated', 'Simulated device (test data)']], dev.source || 'gateway', { disabled: !!existing });
    const proto = select(PROTOCOLS, dev.protocol || 'modbus_tcp');
    const profSel = select([['', '— none (custom) —'], ...profiles.map((p) => [p.id, p.name + (p.status === 'template' ? ' [template]' : p.status === 'starter' ? ' [starter]' : p.status === 'simulated' ? ' [simulated]' : '')])], dev.profile_id || '');
    const name = inp(dev.name), desc = inp(dev.description), loc = inp(dev.location), manu = inp(dev.manufacturer), model = inp(dev.model);
    const cat = select(CATEGORIES, dev.category || 'other');
    const applyProf = h('input', { type: 'checkbox', checked: !existing });
    const profNote = h('div', { class: 'banner hide' });
    const host = inp(conn.host, { placeholder: '192.168.1.10' }), port = num(conn.port ?? 502), slave = num(conn.slave_id ?? 1);
    const serial = inp(conn.serial_port, { placeholder: 'COM3  or  /dev/ttyUSB0' });
    const baud = select([1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200], conn.baud_rate || 9600);
    const parity = select([['N', 'None'], ['E', 'Even'], ['O', 'Odd']], conn.parity || 'N');
    const stop = select([1, 2], conn.stop_bits || 1), dbits = select([8, 7], conn.data_bits || 8);
    const poll = num(conn.poll_interval_ms ?? 1000), tmo = num(conn.timeout_ms ?? 3000), retries = num(conn.retries ?? 2);
    const endpoint = inp(conn.endpoint, { placeholder: 'opc.tcp://host:4840' }), secPol = inp(conn.security_policy || 'None');
    const baseTopic = inp(conn.base_topic), tls = h('input', { type: 'checkbox', checked: !!conn.tls });
    const cu = inp(dev.credentials?.username), cp = h('input', { type: 'password', placeholder: dev.credentials?.has_password ? '(unchanged)' : '', autocomplete: 'new-password' });
    const cert = h('textarea', { rows: 3, placeholder: dev.credentials?.has_certificate ? '(certificate stored — paste to replace)' : '-----BEGIN CERTIFICATE-----' });
    const sections = { tcp: h('div', { class: 'form-grid' }, field('IP address / host name', host), field('TCP port', port), field('Modbus slave (unit) ID', slave)),
      rtu: h('div', { class: 'form-grid' }, field('Serial port on the gateway', serial), field('Baud rate', baud), field('Parity', parity), field('Stop bits', stop), field('Data bits', dbits), field('Modbus slave ID', slave.cloneNode ? num(conn.slave_id ?? 1, { id: 'rtu-slave' }) : slave)),
      mqtt: h('div', { class: 'form-grid' }, field('Broker host', host.cloneNode()), field('Port', num(conn.port ?? 1883)), field('Base topic', baseTopic), field('Use TLS', tls)),
      opcua: h('div', { class: 'form-grid' }, field('Endpoint URL', endpoint), field('Security policy', secPol)) };
    const rtuSlave = sections.rtu.querySelector('#rtu-slave');
    const mqHost = sections.mqtt.querySelector('input'), mqPort = sections.mqtt.querySelectorAll('input')[1];
    const gwNote = h('div', { class: 'small mut' });
    const connBox = h('div'), gwBox = h('div');

    const refresh = () => {
      const sid = Number(siteSel2.value);
      bSel.replaceChildren(h('option', { value: '' }, '— none —'), ...buildings.filter((b) => b.site_id === sid).map((b) => h('option', { value: b.id, selected: b.id === dev.building_id }, b.name)));
      const gws = gateways.filter((g) => g.site_id === sid);
      gwSel.replaceChildren(...gws.map((g) => h('option', { value: g.id, selected: g.id === dev.gateway_id }, g.name + (g.online ? '' : ' (offline)'))));
      const real = src.value === 'gateway';
      gwBox.classList.toggle('hide', !real); connBox.classList.toggle('hide', !real);
      const g = gws.find((x) => String(x.id) === gwSel.value);
      gwNote.textContent = !real ? 'Simulated devices generate labelled test values on the server. No physical connection is made.' : !gws.length ? 'This site has no gateway yet. Create one under Gateways first: real devices are read only by a TBox Edge Gateway on the plant network.' :
        g && !g.protocols.includes(proto.value) && g.last_seen ? 'Warning: this gateway does not currently offer the "' + proto.value + '" driver (offers: ' + (g.protocols.join(', ') || 'none') + ').' : '';
      for (const k of Object.keys(sections)) sections[k].classList.toggle('hide', !(proto.value === 'modbus_tcp' ? k === 'tcp' : proto.value === 'modbus_rtu' ? k === 'rtu' : k === proto.value));
    };
    clear(connBox).append(h('h3', null, 'Connection'), sections.tcp, sections.rtu, sections.mqtt, sections.opcua,
      h('div', { class: 'form-grid' }, field('Polling interval (ms)', poll, 'How often tags are read. Minimum 100.'), field('Timeout (ms)', tmo), field('Failures before "communication lost"', retries, 'Consecutive failed polls minus one.')),
      h('h3', { style: 'margin-top:8px' }, 'Authentication (only if the device or broker needs it)'), h('div', { class: 'form-grid' }, field('Username', cu), field('Password', cp)), field('Certificate / key (PEM)', cert, 'Stored encrypted on the server when the cryptography package is installed.'));
    clear(gwBox).append(h('div', { class: 'form-grid' }, field('Gateway', gwSel)), gwNote);
    siteSel2.onchange = refresh; gwSel.onchange = refresh; proto.onchange = refresh; src.onchange = refresh;

    profSel.onchange = () => {
      const p = profiles.find((x) => x.id === profSel.value);
      profNote.classList.toggle('hide', !p);
      if (!p) return;
      profNote.textContent = p.notes || '';
      profNote.classList.toggle('err', p.status === 'template' || p.status === 'starter');
      if (!existing) {
        if (p.manufacturer) manu.value = p.manufacturer; if (p.model) model.value = p.model; cat.value = p.category;
        if (p.source === 'gateway') { proto.value = p.protocol; src.value = 'gateway'; } else src.value = 'simulated';
        const d = p.defaults || {};
        if (d.port) port.value = d.port; if (d.slave_id) { slave.value = d.slave_id; if (rtuSlave) rtuSlave.value = d.slave_id; }
        if (d.baud_rate) baud.value = d.baud_rate; if (d.parity) parity.value = d.parity; if (d.stop_bits) stop.value = d.stop_bits;
        if (!name.value) name.value = p.name.replace(/^SIMULATED /, '');
        refresh();
      }
    };
    refresh(); profSel.onchange();

    const form = h('div', null,
      h('h3', null, 'Identity'), h('div', { class: 'form-grid' }, field('Device name', name), field('Description', desc), field('Equipment location', loc, 'e.g. Generator room, panel MDB-1')),
      h('div', { class: 'form-grid' }, field('Site', siteSel2), field('Building / facility', bSel), field('Category', cat), field('Manufacturer', manu), field('Model', model)),
      h('h3', null, 'Device profile / register map'), field('Profile', profSel), profNote, existing ? null : h('label', { style: 'display:flex;gap:6px;align-items:center' }, applyProf, 'Create the profile\'s tags with the device'),
      h('h3', { style: 'margin-top:10px' }, 'Communication'), h('div', { class: 'form-grid' }, field('Data source', src), field('Protocol', proto)), gwBox, connBox);

    const save = async () => {
      const real = src.value === 'gateway';
      const c = { poll_interval_ms: Number(poll.value), timeout_ms: Number(tmo.value), retries: Number(retries.value) };
      if (proto.value === 'modbus_tcp') Object.assign(c, { host: host.value, port: Number(port.value), slave_id: Number(slave.value) });
      if (proto.value === 'modbus_rtu') Object.assign(c, { serial_port: serial.value, baud_rate: Number(baud.value), parity: parity.value, stop_bits: Number(stop.value), data_bits: Number(dbits.value), slave_id: Number(rtuSlave.value) });
      if (proto.value === 'mqtt') Object.assign(c, { host: mqHost.value, port: Number(mqPort.value), tls: tls.checked, base_topic: baseTopic.value });
      if (proto.value === 'opcua') Object.assign(c, { endpoint: endpoint.value, security_policy: secPol.value });
      const creds = {}; if (cu.value) creds.username = cu.value; if (cp.value) creds.password = cp.value; if (cert.value) creds.certificate = cert.value;
      const payload = { site_id: Number(siteSel2.value), building_id: bSel.value ? Number(bSel.value) : null, gateway_id: real && gwSel.value ? Number(gwSel.value) : null, name: name.value, description: desc.value,
        location: loc.value, manufacturer: manu.value, model: model.value, category: cat.value, profile_id: profSel.value, source: src.value, protocol: proto.value, connection: real ? c : {},
        apply_profile: applyProf.checked, credentials: Object.keys(creds).length ? creds : undefined };
      try {
        const r = existing ? await put('/api/devices/' + existing.id, payload) : await post('/api/devices', payload);
        toast(existing ? 'Device updated' : 'Device created' + (r.tags_created ? ' with ' + r.tags_created + ' tags' : ''), 'ok'); draw();
      } catch (e) { toast(e.message, 'err'); return false; }
    };
    modal(existing ? 'Edit device' : 'Add device', form, { buttons: [{ label: 'Cancel', onclick: () => {} }, { label: existing ? 'Save changes' : 'Add device', cls: 'pri', onclick: save }] });
  }

  async function diagnostics(id) {
    const load = async () => {
      const d = await get('/api/devices/' + id + '/diagnostics');
      const gw = d.gateway;
      const kv = (pairs) => h('div', { class: 'kv' }, pairs.flatMap(([k, v]) => [h('div', null, k), h('div', null, v ?? '—')]));
      return h('div', { class: 'stack' },
        h('div', { class: 'row' }, statusPill(d.status), d.simulated ? simBadge() : null, h('span', { class: 'mut small' }, 'Last communication: ' + (d.last_comm_ts ? fmtTime(d.last_comm_ts) + ' (' + ago(d.last_comm_ts) + ')' : 'never'))),
        d.last_error ? h('div', { class: 'banner err' }, 'Last error: ' + d.last_error) : null,
        h('div', null, h('h3', null, 'Connection'), kv([['Protocol', d.simulated ? 'simulated (server-generated)' : d.protocol], ...Object.entries(d.connection).map(([k, v]) => [k.replace(/_/g, ' '), String(v)]), ['Enabled', d.enabled ? 'yes' : 'no (disconnected)']])),
        gw ? h('div', null, h('h3', null, 'Gateway'), kv([['Name', gw.name], ['Last seen', ago(gw.last_seen)], ['Version', gw.version], ['Drivers', (gw.protocols || []).join(', ')], ['Buffered records', String(gw.buffer_depth)], ['Clock offset', gw.clock_skew_ms == null ? '—' : gw.clock_skew_ms + ' ms']])) : null,
        h('div', null, h('h3', null, 'Poll statistics (reported by gateway)'), kv([['Latency', d.diagnostics.latency_ms != null ? d.diagnostics.latency_ms + ' ms' : '—'], ['Polls / failures', d.diagnostics.stats ? d.diagnostics.stats.polls + ' / ' + d.diagnostics.stats.failures : '—'], ['In this status since', d.diagnostics.since ? fmtTime(d.diagnostics.since) : '—']])),
        h('div', null, h('h3', null, 'Tag quality'), h('div', { class: 'row' }, Object.entries(d.quality).map(([q, n]) => h('span', { class: 'pill q-' + (q === 'no_data' ? 'bad' : q) }, q.replace('_', ' ') + ': ' + n)))),
        d.active_alarms.length ? h('div', null, h('h3', null, 'Active alarms'), ...d.active_alarms.map((a) => h('div', null, a.message))) : null,
        h('div', null, h('h3', null, 'Recent events'), table([{ label: 'Time', render: (e) => fmtTime(e.ts) }, { label: 'Event', wrap: true, render: (e) => e.message }], d.recent_events, 'None')));
    };
    const holder = h('div', null, 'Loading…');
    const m = modal('Diagnostics', holder, { buttons: [{ label: 'Refresh', onclick: async () => { holder.replaceChildren(await load()); return false; }, close: false }, { label: 'Close', onclick: () => {} }] });
    holder.replaceChildren(await load());
  }

  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' }, ctx.canEdit ? h('button', { class: 'btn pri', onclick: () => deviceForm(null) }, '+ Add device') : null, siteSel, statSel, search), body);
  await draw(); ctx.every(3000, draw);
}

// ------------------------------------------------------------------ tags
export async function tags(box, ctx) {
  const devs = await get('/api/devices');
  if (!devs.length) return box.append(h('div', { class: 'mut' }, 'Add a device first.'));
  const dsel = select(devs.map((d) => [d.id, d.name + ' (' + d.site_name + ')']), ctx.params.device_id || devs[0].id, { onchange: () => draw() });
  const search = h('input', { placeholder: 'Search tags…', style: 'max-width:200px', oninput: () => draw() });
  const body = h('div');
  const curDev = () => devs.find((d) => String(d.id) === dsel.value);

  async function draw() {
    const rows = await get('/api/tags?' + qs({ device_id: dsel.value, q: search.value }));
    const dv = curDev();
    clear(body).append(table([
      { label: 'Tag', render: (t) => [h('b', null, t.name), t.enabled ? null : h('span', { class: 'pill s-offline', style: 'margin-left:6px' }, 'disabled'), h('div', { class: 'small mut' }, t.description)] },
      { label: 'Value', render: (t) => h('span', { class: 'mono' }, t.no_data ? '—' : fmtNum(t.value) + (t.unit ? ' ' + t.unit : '')) },
      { label: 'Quality', render: (t) => qualityPill(t.no_data ? 'bad' : t.quality) }, { label: 'Timestamp', render: (t) => fmtTime(t.ts) },
      { label: 'Address', render: (t) => dv && dv.protocol.startsWith('modbus') ? t.table_type + ' ' + t.address + (t.bit != null ? '.' + t.bit : '') : t.address_ref },
      { label: 'Type', render: (t) => t.data_type + (['float32', 'int32', 'uint32', 'int64', 'uint64', 'float64'].includes(t.data_type) ? ' ' + t.byte_order : '') },
      { label: 'Scale', render: (t) => (t.scale !== 1 || t.offset ? t.scale + 'x' + (t.offset ? '+' + t.offset : '') : '—') },
      { label: 'Poll', render: (t) => (t.poll_interval_ms ? t.poll_interval_ms + ' ms' : 'device') }, { label: 'Log', render: (t) => t.log_interval_s + ' s' },
      { label: 'Limits', render: (t) => [['lolo', t.lolo], ['lo', t.lo], ['hi', t.hi], ['hihi', t.hihi]].filter(([, v]) => v != null).map(([k, v]) => k + ' ' + v).join('  ') || '—' },
      { label: 'Access', render: (t) => t.access }, { label: 'Use', render: (t) => t.aggregation === 'avg' ? 'measurement' : t.aggregation === 'counter' ? 'counter' : 'level' },
      { label: '', render: (t) => h('div', { class: 'row' }, h('a', { class: 'btn sm', href: '#/trends?device_id=' + t.device_id + '&tag_id=' + t.id }, 'Trend'),
        ctx.canEdit ? [canWrite(t) ? h('button', { class: 'btn sm pri', onclick: () => openWrite(t, draw) }, 'Write') : null, h('button', { class: 'btn sm', onclick: () => tagForm(t) }, 'Edit'), h('button', { class: 'btn sm dng', onclick: () => remove(t) }, 'Delete')] : null) }], rows, 'No tags on this device yet.'));
  }
  async function remove(t) {
    if (!(await confirmBox('Delete tag', 'Delete "' + t.name + '"? If it has history it is disabled and its history is kept.', 'Delete', true))) return;
    try { const r = await del('/api/tags/' + t.id); toast(r.disabled ? 'Tag disabled, history kept' : 'Tag deleted', 'ok'); draw(); } catch (e) { toast(e.message, 'err'); }
  }
  function tagForm(t) {
    const dv = curDev(), modbus = dv.protocol.startsWith('modbus'), sim = dv.simulated;
    const v = t || { table_type: 'holding', address: 0, data_type: 'uint16', byte_order: 'ABCD', scale: 1, offset: 0, access: 'R', aggregation: 'avg', log_interval_s: 60, alarm_priority: 'medium', alarm_enabled: 1, deadband: 0, enabled: 1 };
    const f = {
      name: inp(v.name), description: inp(v.description), category: inp(v.category), unit: inp(v.unit),
      table_type: select([['holding', 'Holding register (4x)'], ['input', 'Input register (3x)'], ['coil', 'Coil (0x)'], ['discrete', 'Discrete input (1x)']], v.table_type),
      address: num(v.address, { min: 0, max: 65535 }), address_ref: inp(v.address_ref, { placeholder: 'ns=2;s=Node  /  topic/path' }),
      data_type: select(DATA_TYPES, v.data_type), byte_order: select(ORDERS, v.byte_order), bit: num(v.bit, { min: 0, max: 15, placeholder: 'optional' }),
      scale: num(v.scale), offset: num(v.offset), poll_interval_ms: num(v.poll_interval_ms, { placeholder: 'device default' }), log_interval_s: num(v.log_interval_s, { min: 1 }),
      access: select([['R', 'Read only'], ['RW', 'Read / write (needs remote control enabled in Settings)']], v.access), write_min: num(v.write_min), write_max: num(v.write_max),
      aggregation: select([['avg', 'Measurement (average/min/max)'], ['counter', 'Cumulative counter (energy, fuel used, hours)'], ['level', 'Level (fuel tank: consumption = drops)']], v.aggregation),
      disp_min: num(v.disp_min), disp_max: num(v.disp_max), lolo: num(v.lolo), lo: num(v.lo), hi: num(v.hi), hihi: num(v.hihi), deadband: num(v.deadband),
      alarm_priority: select(['low', 'medium', 'high', 'critical'], v.alarm_priority), alarm_enabled: h('input', { type: 'checkbox', checked: !!v.alarm_enabled }), enabled: h('input', { type: 'checkbox', checked: !!v.enabled }) };
    const simBox = { kind: select(['sine', 'const', 'counter', 'square', 'saw', 'daylight'], 'sine'), json: h('textarea', { rows: 2, placeholder: '{"kind":"sine","base":230,"amp":3,"period_s":60}' }) };
    if (sim && v.sim) simBox.json.value = v.sim;
    const prot = modbus ? h('div', { class: 'form-grid' }, field('Register table', f.table_type), field('Register address (0-based)', f.address), field('Data type', f.data_type), field('Byte / word order', f.byte_order, 'For 32/64-bit values. Check your device manual.'), field('Bit index', f.bit, 'Boolean from one bit of a register'))
      : h('div', { class: 'form-grid' }, field('Node ID / topic', f.address_ref), field('Data type', f.data_type));
    const form = h('div', null, h('h3', null, 'Identity'), h('div', { class: 'form-grid' }, field('Tag name', f.name), field('Description', f.description), field('Category', f.category), field('Engineering unit', f.unit)),
      h('h3', null, 'Protocol & address'), prot,
      h('h3', null, 'Scaling & timing'), h('div', { class: 'form-grid' }, field('Scaling factor', f.scale, 'engineering = raw × factor + offset'), field('Offset', f.offset), field('Polling interval (ms)', f.poll_interval_ms), field('History logging interval (s)', f.log_interval_s, '1 = one-second logging'), field('Usage', f.aggregation), field('Permission', f.access), field('Write: allowed minimum', f.write_min, 'Only used for read/write tags'), field('Write: allowed maximum', f.write_max)),
      h('h3', null, 'Alarm limits'), h('div', { class: 'form-grid' }, field('Low-low', f.lolo), field('Low', f.lo), field('High', f.hi), field('High-high', f.hihi), field('Deadband', f.deadband), field('Priority', f.alarm_priority)),
      h('h3', null, 'Gauge range (optional)'), h('div', { class: 'form-grid' }, field('Display min', f.disp_min), field('Display max', f.disp_max)),
      sim ? [h('h3', null, 'Simulation generator (simulated devices only)'), field('Generator JSON', simBox.json, 'kinds: sine, const, counter, square, saw, daylight')] : null,
      h('div', { class: 'row' }, h('label', { style: 'display:flex;gap:6px;align-items:center;margin:0' }, f.alarm_enabled, 'Alarms enabled'), h('label', { style: 'display:flex;gap:6px;align-items:center;margin:0' }, f.enabled, 'Tag enabled')));
    const save = async () => {
      const p = { device_id: Number(dsel.value) };
      for (const [k, el] of Object.entries(f)) p[k] = el.type === 'checkbox' ? el.checked : el.value;
      if (sim && simBox.json.value.trim()) { try { p.sim = JSON.parse(simBox.json.value); } catch { toast('Generator JSON is not valid', 'err'); return false; } }
      try { t ? await put('/api/tags/' + t.id, p) : await post('/api/tags', p); toast('Tag saved', 'ok'); draw(); } catch (e) { toast(e.message, 'err'); return false; }
    };
    modal(t ? 'Edit tag' : 'Add tag', form, { buttons: [{ label: 'Cancel', onclick: () => {} }, { label: 'Save', cls: 'pri', onclick: save }] });
  }
  const fileIn = h('input', { type: 'file', accept: '.csv,text/csv', class: 'hide', onchange: async (e) => {
    const file = e.target.files[0]; if (!file) return;
    try { const r = await api('POST', '/api/tags/import?device_id=' + dsel.value, await file.text(), { contentType: 'text/csv' });
      modal('Import result', h('div', null, h('p', null, r.created + ' created, ' + r.updated + ' updated, ' + r.errors.length + ' rejected.'), r.errors.length ? table([{ label: 'Row', render: (x) => x.row }, { label: 'Problem', wrap: true, render: (x) => x.error }], r.errors) : null), { buttons: [{ label: 'Close', onclick: () => {} }] }); draw(); }
    catch (ex) { toast(ex.message, 'err'); } e.target.value = ''; } });
  box.append(h('div', { class: 'row', style: 'margin-bottom:10px' }, dsel, search, h('span', { class: 'sp' }),
    ctx.canEdit ? [h('button', { class: 'btn pri', onclick: () => tagForm(null) }, '+ Add tag'), h('button', { class: 'btn', onclick: () => fileIn.click() }, 'Import CSV')] : null,
    h('button', { class: 'btn', onclick: () => download('/api/tags/export?device_id=' + dsel.value, 'tags.csv').catch((e) => toast(e.message, 'err')) }, 'Export CSV'), fileIn), body);
  await draw(); ctx.every(2000, draw);
}
