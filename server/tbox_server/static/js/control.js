// Controlled write dialog. The server, the gateway and the PLC each re-check the request.
import { get, post, session } from './api.js';
import { h, modal, field, select, fmtNum, simBadge } from './ui.js';

export const canWrite = (t) => t.access === 'RW' && !t.simulated && session.user && ['admin', 'engineer'].includes(session.user.role);

export function openWrite(t, onDone) {
  const isBool = t.data_type === 'bool';
  const input = isBool ? select([['1', 'ON (1)'], ['0', 'OFF (0)']], '0') : h('input', { type: 'number', step: 'any', placeholder: 'new value' });
  const confirm = h('input', { type: 'checkbox' });
  const result = h('div');
  const range = (t.write_min != null || t.write_max != null) ? 'Allowed range: ' + (t.write_min ?? '−∞') + ' … ' + (t.write_max ?? '+∞') + (t.unit ? ' ' + t.unit : '') : 'No range limit is configured for this tag.';
  const send = async (e, close) => {
    if (!confirm.checked) { result.replaceChildren(h('div', { class: 'banner err' }, 'Tick the confirmation box first.')); return false; }
    if (!isBool && input.value === '') { result.replaceChildren(h('div', { class: 'banner err' }, 'Enter a value.')); return false; }
    result.replaceChildren(h('div', { class: 'banner' }, 'Sending to the gateway…'));
    try {
      const r = await post('/api/tags/' + t.id + '/write', { value: isBool ? Number(input.value) : Number(input.value), confirm: true });
      for (let i = 0; i < 25; i++) {
        await new Promise((res) => setTimeout(res, 1000));
        const c = await get('/api/commands/' + r.command_id);
        if (c.done) {
          const ok = c.result && c.result.ok;
          result.replaceChildren(h('div', { class: 'banner' + (ok ? '' : ' err') }, (ok ? '✔ ' : '✖ ') + (c.result ? c.result.message : 'Request ' + c.status)));
          if (onDone) onDone();
          return false;
        }
      }
      result.replaceChildren(h('div', { class: 'banner err' }, 'No answer from the gateway within 25 seconds. The write may not have happened; check the value.'));
    } catch (ex) { result.replaceChildren(h('div', { class: 'banner err' }, ex.message)); }
    return false;
  };
  modal('Write to equipment', h('div', null,
    h('div', { class: 'banner err' }, 'This sends a value to real equipment. Safety interlocks and limits remain in the PLC. Every write is audited.'),
    h('div', { class: 'kv', style: 'margin-bottom:10px' }, h('div', null, 'Device'), h('div', null, t.device_name), h('div', null, 'Tag'), h('div', null, t.name),
      h('div', null, 'Current value'), h('div', null, t.no_data ? '—' : fmtNum(t.value) + (t.unit ? ' ' + t.unit : '')), h('div', null, 'Limits'), h('div', null, range)),
    field('New value' + (t.unit ? ' (' + t.unit + ')' : ''), input),
    h('label', { style: 'display:flex;gap:8px;align-items:center;color:var(--text)' }, confirm, 'I confirm this value is safe to send to this equipment now.'), result),
    { small: true, buttons: [{ label: 'Close', onclick: () => {} }, { label: 'Send to device', cls: 'pri', close: false, onclick: send }] });
}
