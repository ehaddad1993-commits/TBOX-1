import { svg, h } from './ui.js';

const PALETTE = ['#ffb000', '#4da3ff', '#2ecc71', '#ff6b9a', '#b36bff', '#4dd6d6'];
export const color = (i) => PALETTE[i % PALETTE.length];

// ---- time-series line chart on <canvas>. series: [{name,color,unit,points:[[ts,avg,min,max,...]],dashed}]
export function lineChart(series, { from, to, height = 260, band = true, limits = null } = {}) {
  const canvas = h('canvas', { class: 'chart' });
  canvas.style.height = height + 'px';
  const tip = h('div', { class: 'small mono mut' }, ' ');
  const wrap = h('div', null, canvas, tip);
  let hover = null;
  function draw() {
    const w = canvas.clientWidth || 600, hh = height, dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr; canvas.height = hh * dpr;
    const c = canvas.getContext('2d'); c.scale(dpr, dpr); c.clearRect(0, 0, w, hh);
    const L = 52, R = 10, T = 8, B = 22, pw = w - L - R, ph = hh - T - B;
    let lo = Infinity, hi = -Infinity;
    for (const s of series) for (const p of s.points) { if (p[1] == null) continue; lo = Math.min(lo, band ? p[2] ?? p[1] : p[1]); hi = Math.max(hi, band ? p[3] ?? p[1] : p[1]); }
    if (limits) for (const v of Object.values(limits)) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (!isFinite(lo)) { c.fillStyle = '#8b9ab0'; c.font = '13px system-ui'; c.fillText('No data in this period', L + 10, hh / 2); return; }
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const X = (t) => L + ((t - from) / (to - from)) * pw, Y = (v) => T + (1 - (v - lo) / (hi - lo)) * ph;
    c.strokeStyle = '#2a3646'; c.fillStyle = '#8b9ab0'; c.font = '11px system-ui'; c.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const v = lo + ((hi - lo) * i) / 4, y = Y(v);
      c.beginPath(); c.moveTo(L, y); c.lineTo(w - R, y); c.stroke();
      c.fillText(Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(Math.abs(v) >= 100 ? 1 : 2), 4, y + 3);
    }
    const span = to - from;
    for (let i = 0; i <= 4; i++) {
      const t = from + (span * i) / 4, d = new Date(t);
      const lab = span > 2 * 86400000 ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
      c.fillText(lab, Math.min(Math.max(X(t) - 18, L), w - 50), hh - 6);
    }
    if (limits) for (const [k, v] of Object.entries(limits)) if (v != null) {
      c.strokeStyle = k.includes('lo') && k !== 'hi' || k === 'lolo' ? '#ff4d4f88' : '#ff4d4f88'; c.setLineDash([4, 4]);
      c.beginPath(); c.moveTo(L, Y(v)); c.lineTo(w - R, Y(v)); c.stroke(); c.setLineDash([]);
      c.fillStyle = '#ff8d8e'; c.fillText(k, w - R - 28, Y(v) - 3);
    }
    for (const s of series) {
      const pts = s.points.filter((p) => p[1] != null);
      if (band && pts.length && pts[0][2] != null) {
        c.fillStyle = s.color + '22'; c.beginPath();
        pts.forEach((p, i) => (i ? c.lineTo(X(p[0]), Y(p[3])) : c.moveTo(X(p[0]), Y(p[3]))));
        for (let i = pts.length - 1; i >= 0; i--) c.lineTo(X(pts[i][0]), Y(pts[i][2]));
        c.closePath(); c.fill();
      }
      c.strokeStyle = s.color; c.lineWidth = 1.8; c.setLineDash(s.dashed ? [5, 4] : []); c.beginPath();
      let gap = true, last = 0;
      const maxGap = (s.bucket || 0) * 3.5;
      for (const p of s.points) {
        if (p[1] == null) { gap = true; continue; }
        if (!gap && maxGap && p[0] - last > maxGap) gap = true;
        gap ? c.moveTo(X(p[0]), Y(p[1])) : c.lineTo(X(p[0]), Y(p[1]));
        gap = false; last = p[0];
      }
      c.stroke(); c.setLineDash([]);
      if (pts.length === 1) { c.fillStyle = s.color; c.fillRect(X(pts[0][0]) - 2, Y(pts[0][1]) - 2, 4, 4); }
    }
    if (hover != null) {
      const t = from + ((hover - L) / pw) * span;
      c.strokeStyle = '#ffffff55'; c.beginPath(); c.moveTo(hover, T); c.lineTo(hover, T + ph); c.stroke();
      const parts = [new Date(t).toLocaleString()];
      for (const s of series) {
        let best = null;
        for (const p of s.points) if (p[1] != null && (best === null || Math.abs(p[0] - t) < Math.abs(best[0] - t))) best = p;
        if (best && Math.abs(best[0] - t) < span / 20) parts.push(s.name + ': ' + best[1].toFixed(3) + (s.unit ? ' ' + s.unit : ''));
      }
      tip.textContent = parts.join('   |   ');
    }
  }
  canvas.addEventListener('mousemove', (e) => { hover = e.clientX - canvas.getBoundingClientRect().left; draw(); });
  canvas.addEventListener('mouseleave', () => { hover = null; tip.textContent = ' '; draw(); });
  canvas.addEventListener('touchmove', (e) => { hover = e.touches[0].clientX - canvas.getBoundingClientRect().left; draw(); }, { passive: true });
  new ResizeObserver(draw).observe(canvas);
  requestAnimationFrame(draw);
  wrap.redraw = draw;
  return wrap;
}

// ---- semicircle gauge (SVG). t: {value,unit,lolo,lo,hi,hihi,disp_min,disp_max}
export function gauge(t) {
  const W = 180, H = 104, cx = 90, cy = 92, r = 74;
  let min = t.disp_min, max = t.disp_max;
  if (min == null || max == null || min >= max) {
    const hiL = t.hihi ?? t.hi, loL = t.lolo ?? t.lo;
    if (hiL != null) { max = hiL * 1.15; min = loL != null ? loL * 0.85 : Math.min(0, t.value ?? 0); }
    else { max = Math.max(1, (t.value ?? 0) * 1.5); min = Math.min(0, t.value ?? 0); }
  }
  const ang = (v) => Math.PI * (1 - Math.min(1, Math.max(0, (v - min) / (max - min))));
  const pt = (a, rr = r) => [cx + rr * Math.cos(a), cy - rr * Math.sin(a)];
  const arc = (v1, v2, col, w = 10) => {
    const [x1, y1] = pt(ang(v1)), [x2, y2] = pt(ang(v2));
    return svg('path', { d: `M${x1} ${y1} A${r} ${r} 0 0 1 ${x2} ${y2}`, fill: 'none', stroke: col, 'stroke-width': w });
  };
  const g = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', style: 'max-width:200px' });
  g.append(arc(min, max, '#2a3646'));
  const zones = [];
  const lo = t.lo, hi = t.hi, lolo = t.lolo, hihi = t.hihi;
  if (lo != null || hi != null || lolo != null || hihi != null) {
    const a = lo ?? min, b = hi ?? max;
    zones.push([a, b, '#1f7a46']);
    if (lo != null) zones.push([lolo ?? min, lo, '#8a6a12']);
    if (hi != null) zones.push([hi, hihi ?? max, '#8a6a12']);
    if (lolo != null) zones.push([min, lolo, '#8a2b2c']);
    if (hihi != null) zones.push([hihi, max, '#8a2b2c']);
    for (const [a1, b1, col] of zones) if (b1 > a1) g.append(arc(Math.max(min, a1), Math.min(max, b1), col));
  }
  if (t.value != null && t.quality !== 'bad') {
    const [nx, ny] = pt(ang(t.value), r - 14);
    g.append(svg('line', { x1: cx, y1: cy, x2: nx, y2: ny, stroke: t.alarm ? '#ff4d4f' : '#ffb000', 'stroke-width': 3, 'stroke-linecap': 'round' }),
      svg('circle', { cx, cy, r: 5, fill: '#e6ebf2' }));
  }
  const mk = (v, x, y, anchor) => svg('text', { x, y, fill: '#8b9ab0', 'font-size': 9, 'text-anchor': anchor }, (Math.abs(v) >= 1000 ? String(Math.round(v)) : String(Number(Number(v).toPrecision(3)))));
  g.append(mk(min, cx - r, cy + 11, 'middle'), mk(max, cx + r, cy + 11, 'middle'));
  return g;
}

export function lamp(on) { return h('span', { class: 'lamp' + (on ? ' on' : '') }); }
