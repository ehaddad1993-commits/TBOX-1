const KEY = 'tbox.token';
export const session = { user: null, get token() { return localStorage.getItem(KEY); }, set token(v) { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } };
export const bus = new EventTarget();

export class ApiError extends Error { constructor(status, message, code) { super(message); this.status = status; this.code = code; } }

export async function api(method, path, body, opts = {}) {
  const headers = {};
  if (session.token) headers.Authorization = 'Bearer ' + session.token;
  let payload;
  if (body !== undefined) {
    if (typeof body === 'string') { payload = body; headers['Content-Type'] = opts.contentType || 'text/plain'; }
    else { payload = JSON.stringify(body); headers['Content-Type'] = 'application/json'; }
  }
  let res;
  try { res = await fetch(path, { method, headers, body: payload }); }
  catch (e) { throw new ApiError(0, 'Cannot reach the TBox server. Check your connection.'); }
  if (opts.raw && res.ok) return res;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    if (res.status === 401 && !path.includes('/auth/login')) bus.dispatchEvent(new Event('unauthorized'));
    if (data && data.code === 'password_change_required') bus.dispatchEvent(new Event('pwchange'));
    throw new ApiError(res.status, (data && data.error) || ('Request failed (' + res.status + ')'), data && data.code);
  }
  return data;
}
export const get = (p) => api('GET', p);
export const post = (p, b) => api('POST', p, b === undefined ? {} : b);
export const put = (p, b) => api('PUT', p, b);
export const del = (p) => api('DELETE', p);

export async function download(path, fallbackName) {
  const res = await api('GET', path, undefined, { raw: true });
  const cd = res.headers.get('Content-Disposition') || '';
  const m = /filename="([^"]+)"/.exec(cd);
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : fallbackName;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
export const qs = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');
