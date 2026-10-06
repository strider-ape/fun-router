/* Small SVG chart kit for fun-router. No dependencies.
 *
 * Charts are drawn at the container's real pixel width (so text never shrinks on a
 * phone) and redrawn on resize. Colours come from CSS variables, so light and dark
 * mode each use their own validated steps. Hover:
 *   - bars, cells, slices: any element with data-tip shows a tooltip
 *   - line/area charts: a crosshair snaps to the nearest point
 * Tooltip text is set with textContent, never as HTML.
 */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const specs = {};       // container id -> {kind, spec}, for redraw on resize
  const hovers = {};      // container id -> {xs, tips, plotTop, plotBottom}
  const esc = (x) => String(x ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const attrTip = (lines) => esc(lines.join('\n'));

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }
  // Round steps: a max of 2×10^n gets 4 steps of 0.5×10^n; anything else 5 steps (50 -> 0,10,..,50).
  function ticks(max) {
    const lead = max / Math.pow(10, Math.floor(Math.log10(max || 1)));
    const count = Math.abs(lead - 2) < 1e-9 ? 4 : 5;
    return Array.from({ length: count + 1 }, (_, i) => (max / count) * i);
  }
  // A bar with only its top corners rounded (data end), anchored flat on the baseline.
  function barPath(x, y, w, h, r = 4) {
    if (h <= 0) return '';
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  function frame(el, width, height, inner) {
    el.innerHTML = `<svg class="chart-svg" xmlns="${NS}" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img">${inner}</svg>`;
  }
  function yAxis(max, fmt, left, right, top, plotH) {
    return ticks(max).map((v) => {
      const y = top + plotH - (v / max) * plotH;
      return `<line class="grid" x1="${left}" x2="${right}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>
        <text class="axis-label" x="${left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${esc(fmt(v))}</text>`;
    }).join('');
  }

  /* Vertical bars, optionally stacked.
     spec: {buckets: [{label, segments: [{v, cls}], state: 'ok'|'missing'|'future', tip: [lines]}],
            fmt: value formatter, every: show every Nth x label, ariaLabel} */
  function bars(el, spec) {
    const width = Math.max(260, el.clientWidth);
    const height = spec.height || 260;
    const left = 64, right = width - 10, top = 14, bottom = height - 30;
    const plotH = bottom - top;
    const n = spec.buckets.length || 1;
    const slot = (right - left) / n;
    const w = Math.max(2, Math.min(42, slot - Math.max(2, slot * 0.28)));
    const max = niceMax(Math.max(0, ...spec.buckets.map((b) => b.segments.reduce((s, x) => s + x.v, 0))));
    let marks = '';
    spec.buckets.forEach((b, i) => {
      const x = left + slot * i + (slot - w) / 2;
      const tip = attrTip(b.tip || []);
      if (b.state === 'missing') {
        marks += `<rect class="bar-missing" x="${x.toFixed(1)}" y="${top}" width="${w.toFixed(1)}" height="${plotH}" rx="4" data-tip="${tip}"/>`;
        return;
      }
      let y = bottom;
      const segs = b.segments.filter((s) => s.v > 0);
      segs.forEach((s, j) => {
        const h = (s.v / max) * plotH;
        const gap = j > 0 ? 2 : 0;  // 2px surface gap between stacked segments
        const segH = Math.max(0, h - gap);
        y -= h;
        const isTop = j === segs.length - 1;
        marks += isTop
          ? `<path class="${s.cls}" d="${barPath(x, y, w, segH)}"/>`
          : `<rect class="${s.cls}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${segH.toFixed(1)}"/>`;
      });
      // A full-height transparent target, wider than the bar, carries the tooltip.
      marks += `<rect class="hit" x="${(left + slot * i).toFixed(1)}" y="${top}" width="${slot.toFixed(1)}" height="${plotH}" data-tip="${tip}"/>`;
    });
    const every = spec.every || 1;
    const labels = spec.buckets.map((b, i) => (i % every === 0 && b.label
      ? `<text class="axis-label" x="${(left + slot * i + slot / 2).toFixed(1)}" y="${height - 10}" text-anchor="middle">${esc(b.label)}</text>` : '')).join('');
    frame(el, width, height, `${yAxis(max, spec.fmt, left, right, top, plotH)}
      <line class="baseline" x1="${left}" x2="${right}" y1="${bottom}" y2="${bottom}"/>${marks}${labels}`);
    el.querySelector('svg').setAttribute('aria-label', spec.ariaLabel || 'Bar chart');
  }

  /* Lines over time. spec: {points: [{t, values: [..]}], series: [{cls, area}], fmt, xfmt, tip(point),
                             ref: {v, label} optional horizontal reference, yMin, yMax} */
  function lines(el, spec) {
    const width = Math.max(260, el.clientWidth);
    const height = spec.height || 240;
    const left = 64, right = width - 12, top = 14, bottom = height - 30;
    const plotH = bottom - top;
    const pts = spec.points;
    if (pts.length < 2) {
      el.innerHTML = `<p class="chart-empty">${esc(spec.emptyText || 'Collecting data…')}</p>`;
      delete hovers[el.id];
      return;
    }
    const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    const all = pts.flatMap((p) => p.values.filter((v) => v != null));
    const lo = spec.yMin != null ? spec.yMin : 0;
    const hi = spec.yMax != null ? spec.yMax : niceMax(Math.max(...all, spec.ref ? spec.ref.v : 0));
    const x = (t) => left + ((t - t0) / Math.max(1, t1 - t0)) * (right - left);
    const y = (v) => top + plotH - ((v - lo) / (hi - lo || 1)) * plotH;
    let marks = '';
    // Break the line where readings are missing (page hidden, recorder off) instead of
    // drawing a straight, made-up segment across the gap.
    const gap = spec.maxGap || Infinity;
    spec.series.forEach((s, si) => {
      const runs = [];
      let run = [];
      pts.forEach((p, i) => {
        if (p.values[si] == null) return;
        if (run.length && p.t - pts[i - 1].t > gap) { runs.push(run); run = []; }
        run.push(p);
      });
      if (run.length) runs.push(run);
      runs.forEach((r) => {
        const coords = r.map((p) => `${x(p.t).toFixed(1)},${y(p.values[si]).toFixed(1)}`);
        if (s.area && r.length > 1) marks += `<path class="${s.cls} area" d="M${x(r[0].t).toFixed(1)},${bottom}L${coords.join('L')}L${x(r[r.length - 1].t).toFixed(1)},${bottom}Z"/>`;
        marks += r.length > 1 ? `<polyline class="${s.cls} line" points="${coords.join(' ')}"/>`
          : `<circle class="${s.cls}" cx="${coords[0].split(',')[0]}" cy="${coords[0].split(',')[1]}" r="2.5"/>`;
      });
    });
    const grid = ticks(hi - lo).map((d) => {
      const v = lo + d;
      return `<line class="grid" x1="${left}" x2="${right}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>
        <text class="axis-label" x="${left - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${esc(spec.fmt(v))}</text>`;
    }).join('');
    const ref = spec.ref ? `<line class="ref" x1="${left}" x2="${right}" y1="${y(spec.ref.v).toFixed(1)}" y2="${y(spec.ref.v).toFixed(1)}"/>
      <text class="ref-label" x="${right - 4}" y="${(y(spec.ref.v) - 6).toFixed(1)}" text-anchor="end">${esc(spec.ref.label)}</text>` : '';
    const xLabels = [0, 0.5, 1].map((f) => {
      const t = t0 + (t1 - t0) * f;
      return `<text class="axis-label" x="${x(t).toFixed(1)}" y="${height - 10}" text-anchor="${f === 0 ? 'start' : f === 1 ? 'end' : 'middle'}">${esc(spec.xfmt(t))}</text>`;
    }).join('');
    frame(el, width, height, `${grid}${ref}<line class="baseline" x1="${left}" x2="${right}" y1="${bottom}" y2="${bottom}"/>${marks}${xLabels}
      <line class="crosshair" x1="0" x2="0" y1="${top}" y2="${bottom}" visibility="hidden"/>
      ${spec.series.map((s) => `<circle class="${s.cls} dot" r="5" visibility="hidden"/>`).join('')}
      <rect class="hit-area" x="${left}" y="${top}" width="${right - left}" height="${plotH}"/>`);
    el.querySelector('svg').setAttribute('aria-label', spec.ariaLabel || 'Line chart');
    hovers[el.id] = { xs: pts.map((p) => x(p.t)), ys: pts.map((p) => p.values.map((v) => (v == null ? null : y(v)))), tips: pts.map(spec.tip) };
  }

  /* Donut. spec: {slices: [{v, cls, label, tip: [lines]}], center: [big, small]} */
  function donut(el, spec) {
    const size = Math.min(260, Math.max(200, el.clientWidth));
    const r = size / 2 - 8, inner = r * 0.62, c = size / 2;
    const total = spec.slices.reduce((s, x) => s + x.v, 0);
    let marks = '';
    if (total > 0) {
      let a = -Math.PI / 2;
      spec.slices.filter((s) => s.v > 0).forEach((s) => {
        const sweep = (s.v / total) * Math.PI * 2;
        const a2 = a + sweep;
        const p = (ang, rad) => `${(c + rad * Math.cos(ang)).toFixed(2)},${(c + rad * Math.sin(ang)).toFixed(2)}`;
        const arc = (from, to) => {
          const large = to - from > Math.PI ? 1 : 0;
          return `<path class="${s.cls} slice" d="M${p(from, r)}A${r},${r} 0 ${large} 1 ${p(to, r)}L${p(to, inner)}A${inner},${inner} 0 ${large} 0 ${p(from, inner)}Z" data-tip="${attrTip(s.tip)}"/>`;
        };
        // A full circle can't be one arc; draw it as two halves.
        marks += sweep >= Math.PI * 2 - 1e-6 ? arc(a, a + Math.PI) + arc(a + Math.PI, a2) : arc(a, a2);
        a = a2;
      });
    } else {
      marks = `<circle class="donut-empty" cx="${c}" cy="${c}" r="${(r + inner) / 2}" fill="none" stroke-width="${r - inner}"/>`;
    }
    frame(el, size, size, `${marks}
      <text class="donut-big" x="${c}" y="${c + 4}" text-anchor="middle">${esc(spec.center[0])}</text>
      <text class="donut-small" x="${c}" y="${c + 24}" text-anchor="middle">${esc(spec.center[1])}</text>`);
    el.querySelector('svg').setAttribute('aria-label', spec.ariaLabel || 'Share chart');
  }

  /* Heatmap. spec: {grid: [rows][cols] numbers, rows: [labels], cols: [labels or ''], fmt, tip(r, c, v)} */
  function heat(el, spec) {
    const width = Math.max(300, el.clientWidth);
    const left = 44, top = 6, colsN = spec.grid[0].length, rowsN = spec.grid.length;
    const cell = Math.max(10, Math.min(52, (width - left - 4) / colsN));
    const gap = 3;
    const height = top + rowsN * cell + 26;
    const max = Math.max(0, ...spec.grid.flat());
    const level = (v) => (v <= 0 || max <= 0 ? 0 : Math.min(5, 1 + Math.floor((v / max) * 4.999)));
    let marks = '';
    spec.grid.forEach((row, r) => {
      marks += `<text class="axis-label" x="${left - 8}" y="${(top + r * cell + cell / 2 + 4).toFixed(1)}" text-anchor="end">${esc(spec.rows[r])}</text>`;
      row.forEach((v, c) => {
        marks += `<rect class="seq-${level(v)}" x="${(left + c * cell).toFixed(1)}" y="${(top + r * cell).toFixed(1)}" width="${(cell - gap).toFixed(1)}" height="${(cell - gap).toFixed(1)}" rx="3" data-tip="${attrTip(spec.tip(r, c, v))}"/>`;
      });
    });
    const labels = spec.cols.map((l, c) => (l ? `<text class="axis-label" x="${(left + c * cell + (cell - gap) / 2).toFixed(1)}" y="${height - 8}" text-anchor="middle">${esc(l)}</text>` : '')).join('');
    frame(el, Math.max(width, left + colsN * cell + 4), height, marks + labels);
    el.querySelector('svg').setAttribute('aria-label', spec.ariaLabel || 'Heatmap');
  }

  const KINDS = { bars, lines, donut, heat };
  function draw(id, kind, spec) {
    const el = document.getElementById(id);
    if (!el) return;
    specs[id] = { kind, spec };
    KINDS[kind](el, spec);
  }

  // --- Tooltip + crosshair (one tooltip element, delegated) ---
  let tipEl = null;
  function tooltip() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'chart-tip';
      tipEl.setAttribute('role', 'status');
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }
  function showTip(lines, cx, cy) {
    const t = tooltip();
    t.replaceChildren(...lines.map((line, i) => {
      const d = document.createElement('div');
      d.textContent = line;
      if (i === 0) d.className = 'chart-tip-head';
      return d;
    }));
    t.style.display = 'block';
    const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
    let left = cx + pad, top = cy + pad;
    if (left + w > window.innerWidth - 8) left = cx - w - pad;
    if (top + h > window.innerHeight - 8) top = cy - h - pad;
    t.style.left = Math.max(8, left) + 'px';
    t.style.top = Math.max(8, top) + 'px';
  }
  function hideTip() {
    if (tipEl) tipEl.style.display = 'none';
    document.querySelectorAll('.chart .crosshair, .chart .dot').forEach((n) => n.setAttribute('visibility', 'hidden'));
  }
  function onMove(e) {
    const tipped = e.target.closest && e.target.closest('.chart [data-tip]');
    if (tipped) {
      const text = tipped.getAttribute('data-tip');
      if (text) { showTip(text.split('\n'), e.clientX, e.clientY); return; }
    }
    const area = e.target.closest && e.target.closest('.chart .hit-area');
    if (area) {
      const box = area.closest('.chart');
      const h = hovers[box.id];
      if (h) {
        const svg = box.querySelector('svg');
        const sx = e.clientX - svg.getBoundingClientRect().left;
        let best = 0;
        h.xs.forEach((x, i) => { if (Math.abs(x - sx) < Math.abs(h.xs[best] - sx)) best = i; });
        const line = svg.querySelector('.crosshair');
        line.setAttribute('x1', h.xs[best]); line.setAttribute('x2', h.xs[best]); line.setAttribute('visibility', 'visible');
        svg.querySelectorAll('.dot').forEach((dot, si) => {
          const y = h.ys[best][si];
          if (y == null) { dot.setAttribute('visibility', 'hidden'); return; }
          dot.setAttribute('cx', h.xs[best]); dot.setAttribute('cy', y); dot.setAttribute('visibility', 'visible');
        });
        showTip(h.tips[best], e.clientX, e.clientY);
        return;
      }
    }
    hideTip();
  }
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerdown', onMove);
  document.addEventListener('scroll', hideTip, true);

  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      Object.entries(specs).forEach(([id, { kind, spec }]) => {
        const el = document.getElementById(id);
        if (el && el.offsetParent !== null) KINDS[kind](el, spec);
      });
    }, 150);
  });

  window.Charts = { draw, hideTip };
})();
