(() => {
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  const esc = (x) => String(x ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* unavailable */ } },
  };

  const POLL_MS = 10000;
  const TABS = ['devices', 'usage', 'internet', 'wifi', 'security', 'tools'];
  const PRESETS = [0, 0.5, 1, 2, 5, 10];
  const BAND_COLOR = { '5': 'c-blue', '2.4': 'c-green' };
  const LOCK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>';

  const data = {};            // last payload per tab
  let tab = 'devices';
  let filter = store.get('frFilter') || 'all';
  let query = '';
  let lastOk = 0;
  let pin = store.get('panelPin') || '';
  let current = null;         // device a dialog is acting on
  let loginDismissed = false;
  let loggedOut = false;      // the user pressed Log out (vs. the router timing us out)
  let explainAll = store.get('frExplain') === '1';
  let caps = [];
  const opened = new Set();   // explainers the user opened; kept open across the 10 s re-renders

  // --- API ---
  async function api(path, body) {
    const headers = { 'X-Panel': '1' };
    if (pin) headers['X-Panel-Pin'] = pin;
    const opts = { headers };
    if (body) { opts.method = 'POST'; headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, opts); } catch (e) {
      throw Object.assign(new Error('The panel server is not running. Start it with: py server.py'), { kind: 'offline' });
    }
    const payload = await res.json().catch(() => ({}));
    if (res.status === 401) throw Object.assign(new Error('The router logged this computer out.'), { kind: 'login' });
    if (res.status === 403 && payload.error === 'pin') throw Object.assign(new Error('PIN needed'), { kind: 'pin' });
    if (!res.ok) throw new Error(payload.error || 'Request failed (' + res.status + ')');
    return payload;
  }

  // --- Formatting ---
  const kbps = (x) => x == null ? '—' : x >= 1000 ? (x / 1000).toFixed(x >= 10000 ? 0 : 1) + ' Mbps' : x + ' kbps';
  const limitText = (kb) => kb >= 1000 ? +(kb / 1000).toFixed(2) + ' Mbps' : kb + ' kbps';
  function duration(sec) {
    if (sec == null) return '—';
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    return d ? d + 'd ' + h + 'h' : h ? h + 'h ' + m + 'm' : m + 'm';
  }
  function bytes(n) {
    if (n == null) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i >= 2 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
  }
  const shortUptime = (u) => (u || '').replace(/\s*days?,\s*/, 'd ').replace(/(\d+):(\d+)$/, '$1h $2m');
  const bars = (rssi) => rssi == null ? 0 : rssi >= -55 ? 4 : rssi >= -65 ? 3 : rssi >= -75 ? 2 : 1;

  // --- Building blocks ---
  const pill = (text, cls = '') => `<span class="pill ${cls}">${esc(text)}</span>`;
  const tag = (text, cls = '') => `<span class="tag ${cls}">${esc(text)}</span>`;
  const panelId = (key) => 'why-' + String(key).replace(/[^a-z0-9]+/gi, '-');
  const isOpen = (key) => explainAll || opened.has(key);

  function why(key, label) {
    return `<button class="why" type="button" data-why="${esc(key)}" aria-expanded="${isOpen(key)}" aria-controls="${panelId(key)}">${esc(label || 'How it works')}</button>`;
  }
  function whyPanel(id, ctx, key = id) {
    const e = window.EXPLAIN && window.EXPLAIN[id];
    if (!e) return '';
    const f = (x) => (typeof x === 'function' ? x(ctx) : x);
    const change = f(e.change), example = f(e.example);
    return `<div class="explain-body" id="${panelId(key)}" data-why-panel="${esc(key)}"${isOpen(key) ? '' : ' hidden'}>
      <div><h4>What it is</h4><p>${f(e.what)}</p></div>
      ${change ? `<div><h4>${esc(e.effect || 'When you change it')}</h4><p>${change}</p></div>` : ''}
      ${example ? `<div><h4>Example</h4><p class="ex">${example}</p></div>` : ''}
    </div>`;
  }
  // A row of topic buttons with their panels underneath: [[explain id, label?, ctx?], ...]
  function whyGroup(items) {
    return `<div class="why-group">
      <div class="why-row">${items.map(([id, label]) => why(id, label || window.EXPLAIN[id].title)).join('')}</div>
      ${items.map(([id, , ctx]) => whyPanel(id, ctx)).join('')}
    </div>`;
  }
  // Label on top, value in a field box, optional "How it works" on the label line.
  function readout(label, value, explainId, ctx, key) {
    const k = explainId ? key || explainId : null;
    return `<div class="readout">
      <div class="readout-top"><span class="label">${esc(label)}</span>${k ? why(k) : ''}</div>
      <div class="value">${value}</div>
      ${k ? whyPanel(explainId, ctx, k) : ''}
    </div>`;
  }
  function card({ title, sub, status = '', color = 'c-yellow', body, foot = '', wide = false, cls = '', attrs = '' }) {
    return `<section class="card${wide ? ' wide' : ''}${cls ? ' ' + cls : ''}" ${attrs}>
      <header class="card-head ${color}"><div class="card-titles"><h3>${esc(title)}</h3>${sub ? `<p>${esc(sub)}</p>` : ''}</div>${status}</header>
      <div class="card-body">${body}</div>
      ${foot ? `<div class="card-foot">${foot}</div>` : ''}
    </section>`;
  }

  // --- Tabs ---
  function selectTab(name) {
    if (!TABS.includes(name)) name = 'devices';
    clearInterval(diagTimer);  // stop any ping/traceroute poll when leaving its view; renderTools resumes it
    clearInterval(liveTimer);
    if (window.Charts) Charts.hideTip();
    tab = name;
    store.set('frTab', name);
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);  // deep link: /#usage
    for (const t of TABS) {
      const on = t === name;
      $('#tab-' + t).setAttribute('aria-selected', String(on));
      $('#tab-' + t).tabIndex = on ? 0 : -1;
      $('#view-' + t).hidden = !on;
    }
    loadTab(name);
  }

  const LOADERS = {
    devices: () => api('/api/state'),
    usage: () => api('/api/stats'),
    internet: () => api('/api/internet'),
    wifi: () => api('/api/wifi'),
    security: () => api('/api/security'),
    tools: async () => ({}),
  };
  const RENDERERS = {
    devices: renderDevices, usage: renderUsage, internet: renderInternet, wifi: renderWifi,
    security: renderSecurity, tools: renderTools,
  };

  async function loadTab(name, quiet) {
    try {
      const payload = await LOADERS[name]();
      data[name] = payload;
      lastOk = Date.now();
      if (name === 'devices') {
        const changed = caps.join() !== (payload.capabilities || []).join();
        caps = payload.capabilities || [];
        applyCaps();
        renderHero(payload.router);  // keep the header current whichever tab is open
        // Caps arrive with the devices state. If another tab is active and was drawn
        // before caps loaded (e.g. the page opened straight onto Tools), redraw it now.
        if (changed && tab !== 'devices') {
          if (tab === 'tools') { $('#tools').innerHTML = ''; renderTools(); }
          else if (data[tab]) RENDERERS[tab](data[tab]);
        }
      }
      banner(null);
      if (tab === name) RENDERERS[name](payload);
    } catch (e) {
      if (e.kind === 'login') { banner(loggedOut ? 'loggedout' : 'login'); if (!loginDismissed && !anyDialogOpen()) openLogin(); }
      else if (e.kind === 'pin') openPin();
      else if (!quiet) banner('error', e.message);
      if (tab === name && !data[name]) renderEmpty(name, e.kind === 'login' ? 'Log in to the router to see this.' : e.message);
    }
  }

  function applyCaps() {
    $('#tab-tools').hidden = !(caps.includes('ping') || caps.includes('traceroute'));
    $('#tab-wifi').hidden = !caps.includes('wifi');
    $('#tab-security').hidden = !caps.includes('security');
    $('#tab-internet').hidden = !caps.includes('internet');
    $('#tab-usage').hidden = !caps.includes('usage');
    // A restored tab may belong to a capability this router lacks; fall back to Devices.
    if ($('#tab-' + tab).hidden) selectTab('devices');
  }

  function renderEmpty(name, msg) {
    const host = { devices: '#grid', usage: '#usage', internet: '#internet', wifi: '#wifi', security: '#security', tools: '#tools' }[name];
    $(host).innerHTML = `<div class="empty">${esc(msg)}</div>`;
  }

  // --- Hero ---
  function renderHero(r, wan) {
    if (!r) return;
    $('#path').textContent = '~/fun-router/' + (r.model || 'router');
    const up = !!(wan || r.wan || {}).up;
    const p = $('#net-pill');
    p.textContent = up ? 'Internet up' : 'Internet down';
    p.className = 'pill big ' + (up ? 'fill-green' : 'fill-red');
    $('#vitals').innerHTML = [['FW', r.firmware], ['Up', shortUptime(r.uptime)], ['CPU', r.cpu], ['Mem', r.memory]]
      .filter((x) => x[1]).map(([k, val]) => `<span class="vital"><b>${esc(k)}</b>${esc(val)}</span>`).join('');
  }

  // --- Devices ---
  function ifaceOf(d) {
    const ifaces = (data.devices && data.devices.interfaces) || [];
    return ifaces.find((i) => i.id === d.iface) || null;
  }
  const nameOf = (d) => d.nickname || d.hostname || 'Unknown device';
  function placeLabel(d) {
    const i = ifaceOf(d);
    return i ? i.label : d.online ? 'Seen on the network' : 'Not connected';
  }
  function placeColor(d) {
    if (d.blocked) return 'c-red';
    const i = ifaceOf(d);
    if (!d.online || !i) return 'c-grey';
    return i.kind === 'wifi' ? BAND_COLOR[i.band] || 'c-grey' : 'c-pink';
  }

  function deviceFilters() {
    const ifaces = (data.devices && data.devices.interfaces) || [];
    const onBand = (band) => (d) => { const i = ifaceOf(d); return !!i && i.band === band; };
    const f = [['all', 'All', () => true]];
    if (ifaces.some((i) => i.band === '5')) f.push(['5', '5 GHz', onBand('5')]);
    if (ifaces.some((i) => i.band === '2.4')) f.push(['24', '2.4 GHz', onBand('2.4')]);
    if (ifaces.some((i) => i.kind === 'lan')) f.push(['wired', 'Cable / AP', (d) => { const i = ifaceOf(d); return !!i && i.kind === 'lan'; }]);
    f.push(['blocked', 'Blocked', (d) => d.blocked]);
    f.push(['limited', 'Limited', (d) => !!(d.limit.down || d.limit.up)]);
    return f;
  }

  function renderDevices(state) {
    renderHero(state.router);
    const devices = state.devices.filter((d) => !d.stale)
      .sort((a, b) => (b.online - a.online) || nameOf(a).localeCompare(nameOf(b)));
    renderStale(state.devices.filter((d) => d.stale));
    const online = devices.filter((d) => d.online);
    const onWifi = online.filter((d) => { const i = ifaceOf(d); return i && i.kind === 'wifi'; });
    $('#stats').innerHTML = [
      ['c-yellow', online.length, 'Online now'],
      ['c-blue', onWifi.length, 'On router Wi-Fi'],
      ['c-pink', online.length - onWifi.length, 'Cable or access point'],
      ['c-red', devices.filter((d) => d.blocked).length, 'Blocked'],
      ['c-orange', devices.filter((d) => d.limit.down || d.limit.up).length, 'Speed-limited'],
    ].map(([c, n, l]) => `<div class="stat ${c}"><b>${n}</b><span>${l}</span></div>`).join('');

    const filters = deviceFilters();
    if (!filters.some((f) => f[0] === filter)) filter = 'all';
    $('#chips').innerHTML = filters.map(([key, label, fn]) =>
      `<button class="chip" type="button" data-filter="${key}" aria-pressed="${filter === key}">${esc(label)}<em>${devices.filter(fn).length}</em></button>`).join('');

    if (!$('#devices-explain').children.length) {
      $('#devices-explain').innerHTML = whyGroup([
        ['device.location'], ['device.online'], ['device.signal', 'Signal and live speed'],
        ['device.privateMac'], ['device.block'], ['device.limit'],
      ]);
    }

    const test = (filters.find((f) => f[0] === filter) || filters[0])[2];
    const q = query.trim().toLowerCase();
    const shown = devices.filter(test).filter((d) => !q ||
      [nameOf(d), d.hostname, d.ip, d.mac].some((x) => x && x.toLowerCase().includes(q)));
    $('#grid').innerHTML = shown.length ? shown.map(deviceCard).join('') : '<div class="empty">No devices match.</div>';
  }

  // Nameless ARP leftovers: not shown as devices, but listed (collapsed) so nothing is hidden.
  let staleOpen = false;
  function renderStale(list) {
    if (!list.length) { $('#stale').innerHTML = ''; return; }
    $('#stale').innerHTML = `<section class="stale">
      <button class="btn sm" type="button" data-stale aria-expanded="${staleOpen}">${staleOpen ? 'Hide' : 'Show'} ${list.length} old address${list.length === 1 ? '' : 'es'}</button>
      <p class="hint">Addresses the router still remembers but hasn't seen traffic from recently, with no name or lease. Usually random MACs a phone has already replaced.</p>
      ${staleOpen ? `<div class="stale-list">${list.map((d) =>
        `<div class="stale-row"><span class="mono">${esc(d.ip || '—')}</span><span class="mono">${esc(d.mac)}</span></div>`).join('')}</div>` : ''}
    </section>`;
  }

  function deviceCard(d) {
    const i = ifaceOf(d);
    const limited = d.limit.down || d.limit.up;
    const status = d.blocked ? pill('Blocked', 'alert') : d.online ? pill('Online', 'on') : pill('Offline', 'off');
    const tags = [];
    if (d.blocked) tags.push(tag('Blocked', 'fill-red'));
    if (d.limit.down) tags.push(tag('↓ max ' + limitText(d.limit.down), 'fill-orange'));
    if (d.limit.up) tags.push(tag('↑ max ' + limitText(d.limit.up), 'fill-orange'));
    if (d.privateMac) tags.push(tag('Private MAC', 'plain'));
    if (d.wifi && d.wifi.uptime != null) tags.push(tag('On for ' + duration(d.wifi.uptime), 'plain'));

    let live = '';
    if (d.wifi) {
      const n = bars(d.wifi.rssi);
      live = `<div class="live">
        <span class="speed"><small>Down now</small><b>↓ ${esc(kbps(d.wifi.downKbps))}</b></span>
        <span class="speed"><small>Up now</small><b>↑ ${esc(kbps(d.wifi.upKbps))}</b></span>
        <span class="sig" title="Signal ${esc(d.wifi.rssi)} dBm, link ${esc(d.wifi.linkMbps)} Mb/s">
          <span class="bars" aria-label="Signal ${n} of 4">${[1, 2, 3, 4].map((x) => `<i class="${x <= n ? 'on' : ''}"></i>`).join('')}</span>
          <span class="dbm">${esc(d.wifi.rssi)} dBm</span>
        </span>
      </div>`;
    } else if (d.online && i && i.kind === 'lan') {
      live = '<p class="hint">Connected through a cable or an access point on this port. Live per-device speed only shows for the router\'s own Wi-Fi.</p>';
    } else if (!d.online) {
      live = '<p class="hint">Not seen recently. It still holds an address lease.</p>';
    }

    const actions = d.protected
      ? `<div class="protected">${LOCK_ICON}<span>${esc(d.protected)}</span></div>`
      : [
        caps.includes('block') ? `<button class="btn ${d.blocked ? 'go' : 'stop'}" type="button" data-act="${d.blocked ? 'unblock' : 'block'}">${d.blocked ? 'Unblock' : 'Block'}</button>` : '',
        caps.includes('limit') ? `<button class="btn" type="button" data-act="limit" ${d.ip ? '' : 'disabled'}>${limited ? 'Edit limit' : 'Limit speed'}</button>` : '',
      ].join('');

    return `<article class="card dev" data-mac="${esc(d.mac)}">
      <header class="card-head ${placeColor(d)}">
        <div class="card-titles"><h3>${esc(nameOf(d))}</h3><p>${esc(placeLabel(d))}</p></div>
        ${status}
      </header>
      <div class="card-body">
        <div class="dev-top">
          <dl class="facts"><dt>IP</dt><dd>${esc(d.ip || '—')}</dd><dt>MAC</dt><dd>${esc(d.mac)}</dd></dl>
          <button class="chip sm" type="button" data-act="rename" aria-label="Rename ${esc(nameOf(d))}">✎ Rename</button>
        </div>
        ${d.nickname && d.hostname ? `<p class="hint">The router calls it ${esc(d.hostname)}.</p>` : ''}
        ${d.olderAddresses && d.olderAddresses.length ? `<p class="hint">Also used ${d.olderAddresses.length} older address${d.olderAddresses.length === 1 ? '' : 'es'} (${d.olderAddresses.map((o) => esc(o.ip)).join(', ')}) — the phone switched to a new random MAC.</p>` : ''}
        ${tags.length ? `<div class="tags">${tags.join('')}</div>` : ''}
        ${live}
      </div>
      ${actions ? `<div class="card-foot">${actions}</div>` : ''}
    </article>`;
  }

  // --- Usage ---
  // Sizes use decimal units (1 GB = 10^9 bytes), the way ISPs count data.
  function fmtBytes(n) {
    if (n == null) return '—';
    const units = [[1e12, 'TB'], [1e9, 'GB'], [1e6, 'MB'], [1e3, 'KB']];
    for (const [size, unit] of units) {
      if (n >= size) { const v = n / size; return (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + ' ' + unit; }
    }
    return Math.round(n) + ' B';
  }
  function fmtRate(bps) {
    if (bps == null) return '—';
    const m = bps / 1e6;
    return (m >= 100 ? m.toFixed(0) : m >= 10 ? m.toFixed(1) : m.toFixed(2)) + ' Mb/s';
  }
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const fmtClock = (ts, sec) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: sec ? '2-digit' : undefined });
  const fmtDay = (ts) => new Date(ts * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
  const RANGE_LABEL = { today: 'Today', week: 'This week', month: 'This month' };

  let usageRange = store.get('frRange') || 'today';
  let usageDir = store.get('frDir') || 'both';
  let usageTable = false;
  let liveTimer = null;
  let livePoints = [];
  const dirVal = (o) => (usageDir === 'down' ? o.down : usageDir === 'up' ? o.up : o.down + o.up);
  const dirWord = () => (usageDir === 'down' ? 'downloaded' : usageDir === 'up' ? 'uploaded' : 'used');

  function coverageNote(r) {
    if (!r.coveredSecs) return 'Not recorded yet';
    const pct = Math.min(100, Math.round((100 * r.coveredSecs) / r.elapsedSecs));
    return pct >= 99 ? 'Fully recorded' : `Recorded ${duration(r.coveredSecs)} of ${duration(r.elapsedSecs)} (${pct}%)`;
  }

  // Colour follows the device, never its rank in the current view: slots are handed out
  // once from this month's ranking, so a device keeps its colour on every range.
  function entityColors(s) {
    const m = s.ranges.month;
    const all = [...m.devices.map((d) => ({ key: d.mac, total: d.down + d.up })), { key: '__lan', total: m.otherDown + m.otherUp }]
      .sort((a, b) => b.total - a.total);
    const map = {};
    all.forEach((e, i) => { map[e.key] = i < 6 ? 'cat-' + (i + 1) : 'cat-other'; });
    return (key) => map[key] || 'cat-other';
  }

  function usageEntities(r) {
    return [
      ...r.devices.map((d) => ({ key: d.mac, name: d.name || d.ip || d.mac, sub: [d.band ? d.band + ' GHz Wi-Fi' : 'Wi-Fi', d.ip].filter(Boolean).join(' · '), down: d.down, up: d.up })),
      { key: '__lan', name: 'Cable & access point', sub: 'Everything not on the router\'s Wi-Fi', down: r.otherDown, up: r.otherUp },
    ].filter((e) => e.down + e.up > 0).sort((a, b) => dirVal(b) - dirVal(a));
  }

  function renderUsage(s) {
    const r = s.ranges[usageRange];
    const colorOf = entityColors(s);
    const boot = s.sinceBoot;
    const tile = (key) => {
      const x = s.ranges[key];
      return `<button class="u-tile" type="button" data-range="${key}" aria-pressed="${usageRange === key}">
        <span class="label">${RANGE_LABEL[key]}</span>
        <b>${esc(fmtBytes(dirVal(x)))}</b>
        <span class="u-split"><span>↓ ${esc(fmtBytes(x.down))}</span><span>↑ ${esc(fmtBytes(x.up))}</span></span>
        <span class="u-note">${esc(coverageNote(x))}</span>
      </button>`;
    };
    const bootTile = boot ? `<div class="u-tile">
        <span class="label">Since router restart</span>
        <b>${esc(fmtBytes(dirVal(boot)))}</b>
        <span class="u-split"><span>↓ ${esc(fmtBytes(boot.down))}</span><span>↑ ${esc(fmtBytes(boot.up))}</span></span>
        <span class="u-note">Exact, from the router${s.uptimeSeconds ? ' · up ' + esc(duration(s.uptimeSeconds)) : ''}</span>
      </div>` : '';
    const chip = (attr, key, label, cur) => `<button class="chip" type="button" data-${attr}="${key}" aria-pressed="${cur === key}">${label}</button>`;

    const ents = usageEntities(r);
    const shown = ents.slice(0, 8);
    const rest = ents.slice(8);
    if (rest.length) shown.push({ key: '__rest', name: `${rest.length} more device${rest.length === 1 ? '' : 's'}`, sub: 'Smaller users combined', down: rest.reduce((a, e) => a + e.down, 0), up: rest.reduce((a, e) => a + e.up, 0) });
    const maxEnt = Math.max(1, ...shown.map(dirVal));
    const devRows = shown.length ? shown.map((e) => `<div class="dev-row" title="↓ ${esc(fmtBytes(e.down))}  ↑ ${esc(fmtBytes(e.up))}">
        <span class="who"><b>${esc(e.name)}</b><small>${esc(e.sub)}</small></span>
        <span class="track"><span class="${e.key === '__rest' ? 'cat-other' : colorOf(e.key)}" style="width:${Math.max(1, (dirVal(e) / maxEnt) * 100)}%"></span></span>
        <span class="amt">${esc(fmtBytes(dirVal(e)))}</span>
      </div>`).join('') : '<p class="chart-empty">No per-device data for this range yet.</p>';

    // Donut: top 5 entities by colour, the rest folded into "Other"
    const top5 = ents.slice(0, 5);
    const others = ents.slice(5);
    const slices = top5.map((e) => ({ key: e.key, label: e.name, v: dirVal(e), cls: colorOf(e.key) }));
    if (others.length) slices.push({ key: '__rest', label: 'Other devices', v: others.reduce((a, e) => a + dirVal(e), 0), cls: 'cat-other' });
    const totalSlices = slices.reduce((a, x) => a + x.v, 0);
    slices.forEach((x) => { x.tip = [x.label, `${fmtBytes(x.v)} ${dirWord()}`, `${totalSlices ? Math.round((100 * x.v) / totalSlices) : 0}% of ${RANGE_LABEL[usageRange].toLowerCase()}`]; });

    // Heatmap busiest slot and other fun facts
    let busiest = null;
    s.heatmap.forEach((row, d) => row.forEach((v, h) => { if (v > 0 && (!busiest || v > busiest.v)) busiest = { d, h, v }; }));
    const busiestText = busiest ? `${DAYS[busiest.d]} ${String(busiest.h).padStart(2, '0')}:00–${String((busiest.h + 1) % 24).padStart(2, '0')}:00` : null;
    const bigBucket = r.buckets.reduce((best, b) => (dirVal(b) > (best ? dirVal(best) : 0) ? b : best), null);
    const bigDay = s.daily.reduce((best, b) => (dirVal(b) > (best ? dirVal(best) : 0) ? b : best), null);
    const avgSpeed = r.coveredSecs ? ((r.down + r.up) * 8) / r.coveredSecs : null;
    const fact = (label, value, note) => `<div class="fact"><span class="label">${esc(label)}</span><b>${esc(value)}</b><small>${esc(note)}</small></div>`;
    const facts = [
      fact('Fastest minute', r.peak ? fmtRate(r.peak.downBps) + ' ↓' : '—', r.peak ? `${fmtDay(r.peak.ts)}, ${fmtClock(r.peak.ts)} · ${fmtRate(r.peak.upBps)} up` : 'Needs a few minutes of recording'),
      fact(usageRange === 'today' ? 'Busiest hour' : 'Busiest day', bigBucket ? fmtBytes(dirVal(bigBucket)) : '—',
        bigBucket ? (usageRange === 'today' ? `${fmtClock(bigBucket.t)}–${fmtClock(bigBucket.t + 3600)}` : fmtDay(bigBucket.t)) : 'Not enough data yet'),
      fact('Download : upload', r.up ? `${(r.down / r.up).toFixed(1)} : 1` : '—', r.up ? `For every byte sent, ${(r.down / r.up).toFixed(1)} came in` : 'No upload recorded yet'),
      fact('Average speed', avgSpeed != null ? fmtRate(avgSpeed) : '—', 'Across the recorded time in this range'),
      fact('Top user', ents[0] ? ents[0].name : '—', ents[0] ? `${fmtBytes(dirVal(ents[0]))} ${dirWord()}` : 'No device data yet'),
      fact('Biggest day (30 days)', bigDay ? fmtBytes(dirVal(bigDay)) : '—', bigDay ? fmtDay(bigDay.t) : 'Not enough data yet'),
      fact('Usual busy slot', busiestText || '—', busiestText ? 'Hour with the most data, last 4 weeks' : 'Builds up over a few days'),
      fact('Recording since', s.recordingSince ? fmtDay(s.recordingSince) : 'Just started', s.recordingSince ? fmtClock(s.recordingSince) : 'First numbers in about a minute'),
    ].join('');

    const legendDir = [usageDir !== 'up' ? '<span><i class="sw s-down"></i>Download</span>' : '', usageDir !== 'down' ? '<span><i class="sw s-up"></i>Upload</span>' : '', '<span><i class="sw missing"></i>Not recorded</span>'].join('');
    const per = usageRange === 'today' ? 'per hour' : 'per day';
    const tableRows = r.buckets.filter((b) => b.t <= s.now).map((b) => `<tr><td>${esc(usageRange === 'today' ? fmtClock(b.t) : fmtDay(b.t))}</td><td>${esc(fmtBytes(b.down))}</td><td>${esc(fmtBytes(b.up))}</td><td>${b.covered ? Math.min(100, Math.round((100 * b.covered) / (usageRange === 'today' ? 3600 : 86400))) + '%' : '—'}</td></tr>`).join('');

    const empty = !s.recordingSince ? `<div class="banner show" style="background:var(--blue)"><p>Recording just started. The first numbers appear in about a minute, the first bar within the hour, and the weekly views fill in as fun-router keeps running.</p></div>` : '';

    $('#usage').innerHTML = `${empty}
      <div class="usage-controls">
        <div class="chips" role="group" aria-label="Time range">${chip('range', 'today', 'Today', usageRange)}${chip('range', 'week', 'This week', usageRange)}${chip('range', 'month', 'This month', usageRange)}</div>
        <div class="chips" role="group" aria-label="Direction">${chip('dir', 'both', 'Both', usageDir)}${chip('dir', 'down', '↓ Download', usageDir)}${chip('dir', 'up', '↑ Upload', usageDir)}</div>
      </div>
      <div class="usage-tiles">${tile('today')}${tile('week')}${tile('month')}${bootTile}</div>
      <div class="cards">
        ${card({ title: 'Live speed', sub: 'Whole connection, updated every 2 seconds', color: 'c-yellow', wide: true,
          status: '<span class="pill busy" id="live-pill">Live</span>',
          body: `<div class="big-live">
              <div><span class="label">Download now</span><b id="live-down">—</b></div>
              <div><span class="label">Upload now</span><b id="live-up">—</b></div>
              <div><span class="label">Fastest this visit</span><b id="live-peak">—</b></div>
            </div>
            <div class="chart" id="ch-live"></div>
            <div class="chart-legend"><span><i class="sw s-down"></i>Download</span><span><i class="sw s-up"></i>Upload</span></div>
            <div>${why('usage.live')}${whyPanel('usage.live')}</div>` })}
        ${card({ title: 'Usage over time', sub: `${RANGE_LABEL[usageRange]}, ${per}`, color: 'c-blue', wide: true,
          status: pill(`${fmtBytes(dirVal(r))} ${dirWord()}`),
          body: `<div class="chart-legend">${legendDir}</div>
            <div class="chart" id="ch-bars"></div>
            <div class="btn-row"><button class="btn sm" type="button" data-table aria-expanded="${usageTable}">${usageTable ? 'Hide table' : 'Show as table'}</button></div>
            ${usageTable ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>${usageRange === 'today' ? 'Hour' : 'Day'}</th><th>Download</th><th>Upload</th><th>Recorded</th></tr></thead><tbody>${tableRows || '<tr><td colspan="4">No data yet</td></tr>'}</tbody></table></div>` : ''}
            <div>${why('usage.recording')}${whyPanel('usage.recording', { since: s.recordingSince ? `${fmtDay(s.recordingSince)} ${fmtClock(s.recordingSince)}` : null })}</div>` })}
        ${card({ title: 'Top devices', sub: `${RANGE_LABEL[usageRange]}, data ${dirWord()}`, color: 'c-green',
          status: ents.length ? pill(`${ents.length} active`) : '',
          body: `<div class="dev-rows">${devRows}</div><div>${why('usage.devices')}${whyPanel('usage.devices')}</div>` })}
        ${card({ title: 'Who used what share', sub: `${RANGE_LABEL[usageRange]}, ${usageDir === 'both' ? 'download + upload' : usageDir === 'down' ? 'download' : 'upload'}`, color: 'c-pink',
          body: `<div class="donut-wrap"><div class="chart" id="ch-donut"></div>
            <div class="chart-legend">${slices.length ? slices.map((x) => `<span><span style="display:inline-flex;align-items:center;gap:8px;min-width:0"><i class="sw ${x.cls}"></i>${esc(x.label)}</span><em>${totalSlices ? Math.round((100 * x.v) / totalSlices) : 0}%</em></span>`).join('') : '<span class="muted">No data for this range yet</span>'}</div></div>` })}
        ${card({ title: 'When you use the internet', sub: 'Last 4 weeks, by weekday and hour', color: 'c-lilac', wide: true,
          body: `<div class="chart" id="ch-heat"></div>
            <div class="seq-legend">Less <i style="background:var(--seq-1)"></i><i style="background:var(--seq-2)"></i><i style="background:var(--seq-3)"></i><i style="background:var(--seq-4)"></i><i style="background:var(--seq-5)"></i> More</div>
            <div>${why('usage.heatmap')}${whyPanel('usage.heatmap', { busiest: busiestText })}</div>` })}
        ${card({ title: 'Fun facts', sub: RANGE_LABEL[usageRange], color: 'c-orange', wide: true, body: `<div class="facts-grid">${facts}</div>` })}
        ${s.optics.length ? card({ title: 'Fibre signal', sub: 'Receive power, last 7 days', color: 'c-grey', wide: true,
          body: `<div class="chart" id="ch-optics"></div><div>${why('fibre.rx', 'What this level means')}${whyPanel('fibre.rx', { rx: s.optics[s.optics.length - 1].rx }, 'fibre.rx:usage')}</div>` }) : ''}
      </div>`;

    // Draw the charts now that their containers exist and have a width
    const hourLabel = (t) => String(new Date(t * 1000).getHours()).padStart(2, '0');
    const span = usageRange === 'today' ? 3600 : 86400;
    Charts.draw('ch-bars', 'bars', {
      ariaLabel: `Data ${dirWord()} ${per}`,
      fmt: fmtBytes,
      every: usageRange === 'month' ? 3 : usageRange === 'today' ? 3 : 1,
      buckets: r.buckets.map((b) => {
        const future = b.t > s.now;
        const missing = !future && !b.covered && !(b.down + b.up);
        const head = usageRange === 'today' ? `${fmtClock(b.t)}–${fmtClock(b.t + 3600)}` : fmtDay(b.t);
        return {
          label: usageRange === 'today' ? hourLabel(b.t) : usageRange === 'week' ? DAYS[(new Date(b.t * 1000).getDay() + 6) % 7] : String(new Date(b.t * 1000).getDate()),
          state: future ? 'future' : missing ? 'missing' : 'ok',
          segments: future ? [] : usageDir === 'up' ? [{ v: b.up, cls: 's-up' }] : usageDir === 'down' ? [{ v: b.down, cls: 's-down' }] : [{ v: b.down, cls: 's-down' }, { v: b.up, cls: 's-up' }],
          tip: future ? [head, 'Still to come'] : missing ? [head, 'Not recorded (fun-router wasn\'t running or couldn\'t read the router)']
            : [head, `↓ ${fmtBytes(b.down)} downloaded`, `↑ ${fmtBytes(b.up)} uploaded`, `Recorded ${Math.min(100, Math.round((100 * b.covered) / span))}% of this ${usageRange === 'today' ? 'hour' : 'day'}`],
        };
      }),
    });
    Charts.draw('ch-donut', 'donut', {
      ariaLabel: 'Share of data by device',
      slices,
      center: [fmtBytes(totalSlices), dirWord()],
    });
    Charts.draw('ch-heat', 'heat', {
      ariaLabel: 'Data by weekday and hour over the last 4 weeks',
      grid: s.heatmap,
      rows: DAYS,
      cols: Array.from({ length: 24 }, (_, h) => (h % 3 === 0 ? String(h).padStart(2, '0') : '')),
      tip: (d, h, v) => [`${DAYS[d]} ${String(h).padStart(2, '0')}:00–${String((h + 1) % 24).padStart(2, '0')}:00`, v ? `${fmtBytes(v)} over 4 weeks` : 'Nothing recorded'],
    });
    if (s.optics.length) {
      const rx = s.optics.map((o) => o.rx).filter((x) => x != null);
      Charts.draw('ch-optics', 'lines', {
        ariaLabel: 'Fibre receive power over the last 7 days',
        points: s.optics.map((o) => ({ t: o.t, values: [o.rx] })),
        series: [{ cls: 's-down' }],
        yMin: Math.floor(Math.min(...rx, -28)), yMax: Math.ceil(Math.max(...rx) + 1),
        ref: { v: -27, label: 'Class B+ limit, −27 dBm' },
        maxGap: 1800,
        fmt: (v) => v.toFixed(1) + ' dBm',
        xfmt: (t) => fmtDay(t),
        tip: (p) => [`${fmtDay(p.t)}, ${fmtClock(p.t)}`, `${p.values[0].toFixed(2)} dBm received`],
        emptyText: 'Fibre readings start appearing after a few minutes.',
      });
    }
    drawLive();
    startLive();
  }

  function drawLive() {
    if (!$('#ch-live')) return;
    const pts = livePoints;
    const last = pts[pts.length - 1];
    $('#live-down').textContent = last ? fmtRate(last.down) : '—';
    $('#live-up').textContent = last ? fmtRate(last.up) : '—';
    $('#live-peak').textContent = pts.length ? fmtRate(Math.max(...pts.map((p) => p.down))) : '—';
    Charts.draw('ch-live', 'lines', {
      ariaLabel: 'Live download and upload speed',
      height: 220,
      maxGap: 7,
      points: pts.map((p) => ({ t: p.t, values: [p.down / 1e6, p.up / 1e6] })),
      series: [{ cls: 's-down', area: true }, { cls: 's-up' }],
      fmt: (v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + ' Mb/s',
      xfmt: (t) => fmtClock(t, true),
      tip: (p) => [fmtClock(p.t, true), `↓ ${fmtRate(p.values[0] * 1e6)}`, `↑ ${fmtRate(p.values[1] * 1e6)}`],
      emptyText: 'Measuring… the graph starts after two readings.',
    });
  }

  function startLive() {
    clearInterval(liveTimer);
    const tick = async () => {
      if (tab !== 'usage' || document.hidden) return;
      try {
        const res = await api('/api/live');
        livePoints = res.points.slice(-150);
        drawLive();
        const p = $('#live-pill');
        if (p) { p.textContent = 'Live'; p.className = 'pill busy'; }
      } catch (e) {
        const p = $('#live-pill');
        if (p) { p.textContent = e.kind === 'login' ? 'Logged out' : 'Paused'; p.className = 'pill off'; }
      }
    };
    liveTimer = setInterval(tick, 2000);
    tick();
  }

  // --- Internet ---
  function renderInternet(info) {
    renderHero(info.router, info.wan);
    const wan = info.wan || {}, f = info.fibre, v6 = info.ipv6 || {};
    const natted = wan.addressKind && wan.addressKind !== 'public';
    const out = [];

    out.push(card({
      title: 'Connection', color: 'c-yellow',
      sub: [wan.protocol, wan.vlan ? 'VLAN ' + wan.vlan : ''].filter(Boolean).join(' · ') || 'WAN',
      status: wan.up ? pill('Up · ' + duration(wan.upSeconds), 'on') : pill('Down', 'alert'),
      body: `<div class="readouts">
        ${readout('Status', esc(wan.up ? 'Connected' : 'Down') + (wan.upSeconds ? ` <span class="muted">for ${esc(duration(wan.upSeconds))}</span>` : ''),
          'wan.session', { vlan: wan.vlan, iface: wan.iface, up: duration(wan.upSeconds), gateway: wan.gateway })}
        ${readout('Connection type', esc([wan.protocol, wan.type].filter(Boolean).join(' · ') || '—'))}
        ${readout('Router uptime', esc(duration(info.uptimeSeconds)))}
      </div>`,
    }));

    out.push(card({
      title: 'Addresses', sub: 'IPv4, IPv6 and DNS', color: 'c-blue',
      status: natted ? pill('Behind CGNAT', 'warn') : wan.addressKind === 'public' ? pill('Public IPv4', 'on') : '',
      body: `<div class="readouts">
        ${readout('WAN IPv4', `<span class="mono">${esc(wan.ip || '—')}</span>` + (natted ? tag(wan.addressKind === 'cgnat' ? 'CGNAT range' : 'Private range', 'fill-orange') : wan.ip ? tag('Public', 'fill-green') : ''),
          'wan.address', { ip: wan.ip, kind: wan.addressKind })}
        ${readout('ISP gateway', `<span class="mono">${esc(wan.gateway || '—')}</span>`)}
        ${readout('IPv6 prefix', `<span class="mono">${esc(v6.prefix || '—')}</span>` + (v6.wan && v6.wan.ip ? tag('Up', 'fill-green') : ''),
          'wan.ipv6', { prefix: v6.prefix, example: v6.lanAddress })}
        ${readout('DNS servers', `<span class="mono">${esc((info.dns || []).slice(0, 2).join(', ') || '—')}</span>`, 'wan.dns', { dns: (info.dns || []).join(', ') })}
      </div>`,
    }));

    if (f) {
      const lo = -30, hi = -8, span = hi - lo;
      const at = Math.max(0, Math.min(100, (f.rxDbm - lo) / span * 100));
      const health = f.rxDbm == null ? '' : f.rxDbm > -25 ? 'Healthy' : f.rxDbm > -27 ? 'Near the limit' : 'Low';
      out.push(card({
        title: 'Fibre', sub: 'GPON optical levels', color: 'c-pink',
        status: f.onuState === 'O5' ? pill('Operating', 'on') : pill(f.onuState || 'Unknown', 'alert'),
        body: `<p class="big-number">${esc(f.rxDbm != null ? f.rxDbm.toFixed(1) : '—')}<small>dBm received · ${esc(health)}</small></p>
          <div class="gauge-wrap" role="img" aria-label="Receive power ${esc(f.rxDbm)} dBm on a scale from -30 to -8">
            <div class="gauge">
              <span class="c-red" style="width:${(-27 - lo) / span * 100}%"></span>
              <span class="c-orange" style="width:${2 / span * 100}%"></span>
              <span class="c-green" style="width:${(hi + 25) / span * 100}%"></span>
            </div>
            ${f.rxDbm != null ? `<i class="needle" style="left:${at}%"></i>` : ''}
          </div>
          <div class="gauge-scale"><span>-30</span><span>-27</span><span>-25</span><span>-8 dBm</span></div>
          <div class="readouts">
            ${readout('Receive power', esc(f.rxDbm != null ? f.rxDbm.toFixed(2) + ' dBm' : '—'), 'fibre.rx', { rx: f.rxDbm })}
            ${readout('Transmit power', esc(f.txDbm != null ? f.txDbm.toFixed(2) + ' dBm' : '—'), 'fibre.tx', { tx: f.txDbm, bias: f.biasMa })}
            ${readout('ONU state', esc(f.onuState || '—'), 'fibre.onu', { state: f.onuState })}
            ${readout('Optics temperature', esc(f.temperatureC != null ? f.temperatureC.toFixed(1) + ' °C' : '—'))}
            ${readout('FEC / HEC errors', esc(f.fecErrors) + ' / ' + esc(f.hecErrors), 'fibre.errors', { fec: f.fecErrors, hec: f.hecErrors })}
          </div>`,
      }));

      const days = info.uptimeSeconds ? info.uptimeSeconds / 86400 : 0;
      const perDay = f.bytesIn && days ? bytes(f.bytesIn / days) : null;
      out.push(card({
        title: 'Data used', sub: 'Since the router last restarted', color: 'c-green',
        status: perDay ? pill('~' + perDay + ' / day') : '',
        body: `<p class="big-number">${esc(bytes(f.bytesIn))}<small>downloaded</small></p>
          <div class="readouts">
            ${readout('Downloaded', esc(bytes(f.bytesIn)), 'usage', { perDay, days: Math.round(days) })}
            ${readout('Uploaded', esc(bytes(f.bytesOut)))}
          </div>`,
      }));
    }

    const ports = info.ports || [];
    const slow = ports.find((p) => p.up && /100/.test(p.speed || ''));
    const worst = (info.interfaces || []).filter((x) => x.txPackets)
      .map((x) => ({ ...x, pct: +(100 * x.txErrors / x.txPackets).toFixed(3) }))
      .sort((a, b) => b.pct - a.pct)[0];
    out.push(card({
      title: 'Ports & interfaces', sub: 'Link speeds and error counters', color: 'c-lilac',
      status: pill(`${ports.filter((p) => p.up).length} of ${ports.length} ports up`),
      body: `<div class="readouts">
        ${ports.map((p) => readout(p.name, p.up
          ? esc(p.speed + ' ' + (p.duplex || '')) + (/100/.test(p.speed || '') ? tag('Fast Ethernet', 'fill-orange') : tag('Gigabit', 'fill-green'))
          : '<span class="muted">Not connected</span>')).join('')}
        ${worst ? readout('Most Wi-Fi/LAN errors', esc(`${worst.name}: ${worst.txErrors} TX errors (${worst.pct}%)`), 'iface.errors', { worst }) : ''}
      </div>
      <div>${why('lan.ports', 'About port speeds')}${whyPanel('lan.ports', { slow: slow && slow.name })}</div>`,
    }));

    $('#internet').innerHTML = out.join('');
  }

  // --- Wi-Fi ---
  function channelMap(band, radios, neighbours) {
    const nets = [
      ...radios.filter((r) => r.band === band && r.channel).map((r) => ({ ch: r.channel, w: r.widthMhz || 20, sig: 95, cls: 'mine', label: r.ssid })),
      ...neighbours.filter((n) => n.channel && (band === '2.4' ? n.channel <= 14 : n.channel > 14))
        .map((n) => ({ ch: n.channel, w: n.widthMhz || 20, sig: n.signal || 10, cls: n.yours ? 'yours' : 'other', label: n.ssid })),
    ];
    if (!nets.length) return '<p class="hint">Nothing seen on this band in the last scan.</p>';
    const ticks = band === '2.4' ? [1, 6, 11, 13] : [36, 52, 100, 116, 132, 149, 165];
    const W = 640, H = 170, pad = 14, base = H - 26;
    const lo = band === '2.4' ? -1 : 30, hi = band === '2.4' ? 15 : 171;
    const x = (ch) => pad + (ch - lo) / (hi - lo) * (W - pad * 2);
    const wpx = (mhz) => Math.max(12, (mhz / 5) / (hi - lo) * (W - pad * 2));
    const rects = nets.sort((a, b) => a.sig - b.sig).map((n) => {
      const h = Math.max(8, (n.sig / 100) * (base - 12));
      const w = wpx(n.w);
      return `<rect class="net ${n.cls}" x="${(x(n.ch) - w / 2).toFixed(1)}" y="${(base - h).toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" rx="5"><title>${esc(n.label || 'Hidden network')} · channel ${n.ch} · ${n.w} MHz${n.cls === 'other' ? ' · ' + n.sig + '% signal' : ' · yours'}</title></rect>`;
    }).join('');
    const labels = ticks.map((ch) => `<text class="tick" x="${x(ch).toFixed(1)}" y="${H - 8}" text-anchor="middle">${ch}</text>`).join('');
    return `<svg class="chanmap" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(band)} GHz channel usage">
      <line class="axis" x1="${pad}" y1="${base}" x2="${W - pad}" y2="${base}"/>${rects}${labels}</svg>`;
  }

  function renderWifi(info) {
    const radios = info.radios || [], neighbours = info.neighbours || [];
    const out = radios.map((r) => {
      const overlaps = neighbours.filter((n) => !n.yours && n.channel &&
        Math.abs(n.channel - r.channel) < (r.band === '2.4' ? 5 : 4)).length;
      const secTag = /WPA3/.test(r.security) ? tag('Strong', 'fill-green') : /WPA2/.test(r.security) ? tag('Good', 'fill-green') : tag('Weak', 'fill-red');
      const wpsValue = r.wps.enabled
        ? 'On' + (r.wps.defaultPin ? ' · factory PIN' : '') + tag(r.wps.defaultPin ? 'Turn off' : 'Consider off', 'fill-red')
        : 'Off' + tag('Good', 'fill-green');
      return card({
        title: `${r.band} GHz Wi-Fi`, color: BAND_COLOR[r.band] || 'c-grey',
        sub: [r.ssid ? `“${r.ssid}”` : '', r.generation].filter(Boolean).join(' · '),
        status: !r.enabled ? pill('Off', 'off') : pill(`${r.clients} client${r.clients === 1 ? '' : 's'}`, r.clients ? 'on' : ''),
        body: `<div class="readouts">
          ${readout('Channel', esc(r.channel) + (r.autoChannel ? tag('Auto', 'fill-blue') : '') + (overlaps ? tag(`Overlaps ${overlaps}`, 'fill-orange') : ''),
            'wifi.channel', { band: r.band, channel: r.channel, width: r.widthMhz, overlaps }, 'wifi.channel:' + r.id)}
          ${readout('Channel width', esc(r.widthMhz + ' MHz'), 'wifi.width', { band: r.band, width: r.widthMhz, sideband: r.sideband }, 'wifi.width:' + r.id)}
          ${readout('Standard', esc(r.standard || '—'), 'wifi.standard', { standard: r.standard, generation: r.generation }, 'wifi.standard:' + r.id)}
          ${readout('Transmit power', esc(r.powerPercent + '%'), 'wifi.power', { power: r.powerPercent }, 'wifi.power:' + r.id)}
          ${readout('Security', esc([r.security, r.cipher].filter(Boolean).join(' · ')) + secTag, 'wifi.security', { security: r.security, cipher: r.cipher }, 'wifi.security:' + r.id)}
          ${readout('Protected management frames', esc(r.pmf || '—') + (r.pmf === 'off' ? tag('Off', 'fill-orange') : ''), 'wifi.pmf', { pmf: r.pmf }, 'wifi.pmf:' + r.id)}
          ${readout('WPS', wpsValue, 'wifi.wps', { enabled: r.wps.enabled, pin: r.wps.pin, defaultPin: r.wps.defaultPin }, 'wifi.wps:' + r.id)}
          ${readout('Guest networks', esc(`${r.guestNetworks.used} of ${r.guestNetworks.slots} in use`), 'wifi.guest', r.guestNetworks, 'wifi.guest:' + r.id)}
        </div>`,
      });
    });

    const byCh = {};
    neighbours.forEach((n) => { if (n.channel && !n.yours) byCh[n.channel] = (byCh[n.channel] || 0) + 1; });
    const busiest = Object.entries(byCh).sort((a, b) => b[1] - a[1])[0];
    const maps = ['2.4', '5'].filter((b) => radios.some((r) => r.band === b))
      .map((b) => `<div class="band-map"><span class="label">${b} GHz band</span>${channelMap(b, radios, neighbours)}</div>`).join('');
    out.push(card({
      title: 'Nearby networks', sub: 'Your channels against the neighbours', color: 'c-pink', wide: true,
      status: pill(`${neighbours.filter((n) => !n.yours).length} nearby`),
      body: `${maps}
        <div class="legend"><span><i class="c-yellow"></i>This router</span><span><i class="c-pink"></i>Your other access points</span><span><i class="c-grey"></i>Neighbours (height = signal)</span></div>
        <div>${why('wifi.neighbours', 'How to read this')}${whyPanel('wifi.neighbours', { busiest: busiest ? +busiest[0] : null })}</div>`,
    }));
    $('#wifi').innerHTML = out.join('');
  }

  // --- Security ---
  function renderSecurity(info) {
    const checks = info.checks || [];
    const counts = { bad: 0, warn: 0, info: 0, good: 0 };
    checks.forEach((c) => { counts[c.level]++; });
    const badge = $('#sec-count');
    badge.hidden = false;
    badge.textContent = counts.bad + counts.warn || '✓';
    badge.className = 'count' + (counts.bad + counts.warn ? '' : ' ok');

    const LEVEL = { bad: ['Act now', 'fill-red'], warn: ['Look into', 'fill-orange'], info: ['Good to know', 'fill-blue'], good: ['All good', 'fill-green'] };
    $('#security').innerHTML = `
      <div class="stats">
        <div class="stat c-red"><b>${counts.bad}</b><span>Act now</span></div>
        <div class="stat c-orange"><b>${counts.warn}</b><span>Look into</span></div>
        <div class="stat c-blue"><b>${counts.info}</b><span>Good to know</span></div>
        <div class="stat c-green"><b>${counts.good}</b><span>All good</span></div>
      </div>
      ${whyGroup([['sec.overview']])}
      <div class="checks">${checks.map((c) => `<article class="check">
        ${pill(LEVEL[c.level][0], LEVEL[c.level][1])}
        <h3>${esc(c.title)}</h3>
        <p>${esc(c.detail)}</p>
      </article>`).join('')}</div>`;
  }

  // --- Tools ---
  let diagTimer = null;
  let diagKind = null;
  let diagDone = true;
  const TOOL = {
    ping: { title: 'Ping', verb: 'Ping', color: 'c-yellow', sub: 'From the router to any host', explain: 'tool.ping',
      desc: 'Sends four ICMP echo requests from the router itself and times each reply. Your own Wi-Fi and computer are not in the path.' },
    traceroute: { title: 'Traceroute', verb: 'Trace', color: 'c-pink', sub: 'Every hop on the way to a host', explain: 'tool.traceroute',
      desc: 'Sends probes with a rising hop limit, so each router on the path reports back. Shows where the delay comes from.' },
  };

  function renderTools() {
    if (!$('#tools').children.length) {
      const kinds = Object.keys(TOOL).filter((k) => caps.includes(k));
      $('#tools').innerHTML = kinds.map(toolCard).join('') || '<div class="empty">This router has no diagnostics.</div>';
    }
    if (diagKind && !diagDone) pollDiag();  // a run started before leaving the tab is still going
  }

  function toolCard(kind) {
    const t = TOOL[kind];
    const gw = data.devices && data.devices.router && data.devices.router.wan && data.devices.router.wan.gateway;
    const quick = [['1.1.1.1', '1.1.1.1'], ['8.8.8.8', '8.8.8.8'], ['google.com', 'google.com']];
    if (gw) quick.push([gw, 'ISP gateway']);
    return card({
      title: t.title, sub: t.sub, color: t.color, attrs: `data-tool="${kind}"`,
      status: `<span class="pill" data-tool-pill="${kind}">Idle</span>`,
      body: `<p class="desc">${esc(t.desc)}</p>
        <form class="tool-form" data-kind="${kind}">
          <label class="label" for="host-${kind}">Host *</label>
          <input class="input" id="host-${kind}" name="host" placeholder="1.1.1.1 or google.com" autocomplete="off" spellcheck="false" inputmode="url">
          <div class="chip-row">${quick.map(([host, label]) => `<button class="chip sm" type="button" data-host="${esc(host)}">${esc(label)}</button>`).join('')}</div>
          <div class="btn-row"><button class="btn go" type="submit">▶ ${esc(t.verb)}</button></div>
        </form>
        <div data-sum="${kind}"></div>
        <div class="log-head"><span class="label">Live log</span><button class="btn sm" type="button" data-clear="${kind}">Clear view</button></div>
        <pre class="log" data-out="${kind}">${placeholderLine(t.verb)}</pre>
        <div>${why(t.explain)}${whyPanel(t.explain)}</div>`,
    });
  }
  const placeholderLine = (verb) => `<span class="placeholder">Nothing has run yet. Press ▶ ${esc(verb)}.</span>`;

  function setToolPill(kind, text, cls) {
    const p = $(`[data-tool-pill="${kind}"]`);
    if (p) { p.textContent = text; p.className = 'pill ' + cls; }
  }

  async function runDiag(kind, host) {
    const out = $(`[data-out="${kind}"]`), sum = $(`[data-sum="${kind}"]`);
    setToolPill(kind, 'Starting', 'busy');
    sum.innerHTML = '';
    out.innerHTML = `<span class="placeholder">Asking the router to ${kind === 'ping' ? 'ping' : 'trace'} ${esc(host)}…</span>`;
    try {
      await api('/api/diag', { kind, host });
    } catch (e) {
      setToolPill(kind, 'Failed', 'alert');
      if (e.kind === 'login') { banner('login'); openLogin(); }
      out.innerHTML = `<span class="err">${esc(e.message)}</span>`;
      return;
    }
    diagKind = kind;
    diagDone = false;
    setToolPill(kind, 'Running', 'busy');
    pollDiag();
  }

  function pollDiag() {
    clearInterval(diagTimer);
    const tick = async () => {
      let r;
      try { r = await api('/api/diag'); } catch (e) { clearInterval(diagTimer); return; }
      if (!r.kind) return;
      renderDiag(r);
      if (r.done) {
        clearInterval(diagTimer);
        diagDone = true;
        setToolPill(r.kind, 'Done', 'on');
      }
    };
    diagTimer = setInterval(tick, 1000);
    tick();
  }

  function renderDiag(r) {
    const out = $(`[data-out="${r.kind}"]`), sum = $(`[data-sum="${r.kind}"]`);
    if (!out) return;
    const lines = r.lines || [];
    if (lines.length) out.textContent = lines.join('\n') + (r.done ? '' : '\n…');
    sum.innerHTML = r.kind === 'ping' ? pingSummary(lines) : hopList(lines);
  }

  function pingSummary(lines) {
    const loss = ((lines.find((l) => /packet loss/.test(l)) || '').match(/(\d+)% packet loss/) || [])[1];
    const rtt = (lines.find((l) => /min\/avg\/max/.test(l)) || '').match(/=\s*([\d.]+)\/([\d.]+)\/([\d.]+)/);
    const times = lines.map((l) => (l.match(/time=([\d.]+)/) || [])[1]).filter(Boolean).map(Number);
    if (!rtt && loss == null && !times.length) return '';
    const avg = rtt ? rtt[2] : (times.reduce((a, b) => a + b, 0) / times.length).toFixed(1);
    return `<dl class="ping-sum">
      <div><dt>Min ms</dt><dd>${rtt ? esc(rtt[1]) : '—'}</dd></div>
      <div><dt>Avg ms</dt><dd>${esc(avg)}</dd></div>
      <div><dt>Max ms</dt><dd>${rtt ? esc(rtt[3]) : '—'}</dd></div>
      <div><dt>Loss</dt><dd>${loss != null ? esc(loss) + '%' : '—'}</dd></div>
    </dl>`;
  }

  function hopList(lines) {
    const hops = lines.map((l) => l.match(/^\s*(\d+)\s+(.*?)\s+([\d.]+)\s*ms/)).filter(Boolean);
    if (!hops.length) return '';
    const max = Math.max(1, ...hops.map((h) => +h[3]));
    return `<div class="hops">${hops.map((h) => `<div class="hop">
      <b>${esc(h[1])}</b><span class="host" title="${esc(h[2])}">${esc(h[2])}</span><span class="ms">${esc(h[3])} ms</span>
      <span class="bar-track"><span class="bar" style="width:${Math.max(4, +h[3] / max * 100)}%"></span></span>
    </div>`).join('')}</div>`;
  }

  // --- Banner / toast ---
  function banner(kind, msg) {
    const el = $('#banner');
    if (!kind) { el.classList.remove('show'); return; }
    $('#banner-text').textContent = kind === 'login'
      ? 'The router logged this computer out, so the panel can\'t read it right now.'
      : kind === 'loggedout' ? 'You logged out of the router. Log in again to see live data.' : msg;
    const btn = $('#banner-btn');
    const needsLogin = kind === 'login' || kind === 'loggedout';
    btn.textContent = needsLogin ? 'Log in' : 'Try again';
    btn.onclick = needsLogin ? () => { loginDismissed = false; openLogin(); } : () => loadTab(tab);
    el.classList.add('show');
  }
  function toast(msg, bad) {
    const el = document.createElement('div');
    el.className = 'toast' + (bad ? ' bad' : '');
    el.textContent = msg;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), bad ? 7000 : 4000);
  }
  const anyDialogOpen = () => $$('dialog').some((d) => d.open);

  // --- Device actions ---
  async function run(button, path, body) {
    const label = button.innerHTML;
    button.disabled = true;
    button.textContent = 'Working…';
    try {
      const res = await api(path, body);
      if (res.message) toast(res.message);
      await loadTab('devices');
      return true;
    } catch (e) {
      if (e.kind === 'login') { banner('login'); openLogin(); }
      else if (e.kind === 'pin') openPin();
      else toast(e.message, true);
      return false;
    } finally {
      button.disabled = false;
      button.innerHTML = label;
    }
  }

  const findDevice = (mac) => ((data.devices && data.devices.devices) || []).find((d) => d.mac === mac);

  function openConfirm(d, act) {
    current = d;
    const block = act === 'block';
    $('#confirm-head').className = 'dlg-head ' + (block ? 'c-red' : 'c-green');
    $('#confirm-title').textContent = (block ? 'Block ' : 'Unblock ') + nameOf(d) + '?';
    $('#confirm-text').textContent = block
      ? 'It loses internet access until you unblock it. It stays on the Wi-Fi and can still reach devices on your network.'
      : 'Internet access comes back right away.';
    const ok = $('#confirm-ok');
    ok.textContent = block ? 'Block it' : 'Unblock';
    ok.className = 'btn ' + (block ? 'stop' : 'go');
    ok.dataset.act = act;
    $('#dlg-confirm').showModal();
  }
  $('#dlg-confirm').addEventListener('close', () => {
    if ($('#dlg-confirm').returnValue !== 'ok' || !current) return;
    const act = $('#confirm-ok').dataset.act;
    const btn = $(`.card[data-mac="${CSS.escape(current.mac)}"] [data-act="${act}"]`) || $('#confirm-ok');
    run(btn, '/api/' + act, { mac: current.mac });
  });

  const limitChoice = { down: 0, up: 0 };
  function buildPresets(dir) {
    const box = $(`fieldset[data-dir="${dir}"] .presets`);
    box.innerHTML = PRESETS.map((v) => `<button class="chip" type="button" data-rate="${v}">${v ? v + '' : 'Off'}</button>`).join('') +
      `<input class="input sm" type="number" min="0.256" step="0.1" placeholder="Custom" aria-label="Custom ${dir === 'down' ? 'download' : 'upload'} limit in Mbps"><span class="unit">Mbps</span>`;
    box.addEventListener('click', (e) => {
      const b = e.target.closest('[data-rate]');
      if (!b) return;
      setLimit(dir, Number(b.dataset.rate));
      box.querySelector('input').value = '';
    });
    box.querySelector('input').addEventListener('input', (e) => setLimit(dir, Number(e.target.value) || 0, true));
  }
  function setLimit(dir, mbps, fromInput) {
    limitChoice[dir] = mbps;
    $(`fieldset[data-dir="${dir}"] .presets`).querySelectorAll('[data-rate]').forEach((b) =>
      b.setAttribute('aria-pressed', String(!fromInput && Number(b.dataset.rate) === mbps)));
  }
  function openLimit(d) {
    current = d;
    $('#limit-title').textContent = 'Limit ' + nameOf(d);
    for (const dir of ['down', 'up']) {
      const mbps = d.limit[dir] ? d.limit[dir] / 1000 : 0;
      const isPreset = PRESETS.includes(mbps);
      setLimit(dir, mbps, !isPreset);
      $(`fieldset[data-dir="${dir}"] input`).value = isPreset ? '' : (mbps || '');
    }
    $('#limit-note').textContent = `Applies to ${d.ip}. If the router gives this device a different IP later, the limit stays on the old one. Minimum ${data.devices.minLimitKbps} kbps.`;
    $('#limit-remove').hidden = !(d.limit.down || d.limit.up);
    $('#dlg-limit').showModal();
  }
  // Submit (the Apply button, or Enter in the custom box) applies the limit.
  $('#limit-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const down = Math.round(limitChoice.down * 1000), up = Math.round(limitChoice.up * 1000);
    if (!down && !up) { toast('Pick a download or upload limit, or use Remove limit.', true); return; }
    const min = data.devices.minLimitKbps;
    if ((down && down < min) || (up && up < min)) { toast(`Limits must be at least ${min} kbps.`, true); return; }
    if (await run($('#limit-apply'), '/api/limit', { mac: current.mac, down, up })) $('#dlg-limit').close();
  });
  $('#limit-remove').addEventListener('click', async (e) => {
    if (await run(e.currentTarget, '/api/unlimit', { mac: current.mac })) $('#dlg-limit').close();
  });

  function openRename(d) {
    current = d;
    $('#rename-input').value = d.nickname || d.hostname || '';
    $('#rename-note').textContent = `The router calls it “${d.hostname || 'no name'}” · ${d.mac}. Names are saved on this computer only.`;
    $('#dlg-rename').showModal();
  }
  $('#rename-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('#rename-input').value.trim();
    if (await run($('#rename-save'), '/api/name', { mac: current.mac, name: name === current.hostname ? '' : name })) $('#dlg-rename').close();
  });
  $('#rename-reset').addEventListener('click', async (e) => {
    if (await run(e.currentTarget, '/api/name', { mac: current.mac, name: '' })) $('#dlg-rename').close();
  });

  // --- Login / PIN ---
  function openLogin() { if (!$('#dlg-login').open) { closeAll(); $('#dlg-login').showModal(); } }
  function openPin() { if (!$('#dlg-pin').open) { closeAll(); $('#dlg-pin').showModal(); } }
  function closeAll() { $$('dialog[open]').forEach((d) => d.close()); }
  // Cancel / Later are plain buttons (not submit), so pressing Enter in a field
  // always triggers the dialog's main action instead of the first button in the footer.
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-close]');
    if (!b) return;
    if (b.id === 'login-later') loginDismissed = true;
    b.closest('dialog').close('cancel');
  });
  $('#dlg-login').addEventListener('cancel', () => { loginDismissed = true; });  // Escape key
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const ok = await run($('#login-go'), '/api/login', { username: $('#login-user').value, password: $('#login-pass').value });
    $('#login-pass').value = '';
    if (ok) { $('#dlg-login').close(); loginDismissed = false; loggedOut = false; toast('Logged in to the router'); loadTab(tab); }
  });
  $('#pin-form').addEventListener('submit', (e) => {
    e.preventDefault();
    pin = $('#pin-input').value.trim();
    store.set('panelPin', pin);
    $('#dlg-pin').close();
    loadTab(tab);
  });

  // --- Wiring ---
  $('#grid').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const d = findDevice(btn.closest('.card').dataset.mac);
    if (!d) return;
    const act = btn.dataset.act;
    if (act === 'block' || act === 'unblock') openConfirm(d, act);
    else if (act === 'limit') openLimit(d);
    else if (act === 'rename') openRename(d);
  });
  $('#stale').addEventListener('click', (e) => {
    if (!e.target.closest('[data-stale]')) return;
    staleOpen = !staleOpen;
    if (data.devices) renderStale(data.devices.devices.filter((d) => d.stale));
  });
  $('#usage').addEventListener('click', (e) => {
    const r = e.target.closest('[data-range]');
    const d = e.target.closest('[data-dir]');
    const t = e.target.closest('[data-table]');
    if (r) { usageRange = r.dataset.range; store.set('frRange', usageRange); }
    else if (d) { usageDir = d.dataset.dir; store.set('frDir', usageDir); }
    else if (t) usageTable = !usageTable;
    else return;
    if (data.usage) renderUsage(data.usage);
  });
  $('#chips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    filter = b.dataset.filter;
    store.set('frFilter', filter);
    if (data.devices) renderDevices(data.devices);
  });
  $('#q').addEventListener('input', (e) => { query = e.target.value; if (data.devices) renderDevices(data.devices); });
  $('#refresh').addEventListener('click', () => loadTab(tab));
  $('#logout').addEventListener('click', async (e) => {
    const btn = e.currentTarget, label = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Logging out…';
    try {
      await api('/api/logout', {});
      loginDismissed = true;  // don't pop the login dialog on the next poll; the banner offers it
      loggedOut = true;
      const p = $('#net-pill');
      p.textContent = 'Logged out';
      p.className = 'pill big fill-grey';
      toast('Logged out of the router');
    } catch (err) {
      if (err.kind !== 'login') toast(err.message, true); else loginDismissed = true;  // already logged out
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
      banner('loggedout');
    }
  });

  $('.tabs').addEventListener('click', (e) => { const t = e.target.closest('[data-tab]'); if (t) selectTab(t.dataset.tab); });
  $('.tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const vis = TABS.filter((t) => !$('#tab-' + t).hidden);
    const i = vis.indexOf(tab);
    const next = vis[(i + (e.key === 'ArrowRight' ? 1 : vis.length - 1)) % vis.length];
    selectTab(next);
    $('#tab-' + next).focus();
  });

  $('#tools').addEventListener('submit', (e) => {
    const form = e.target.closest('[data-kind]');
    if (!form) return;
    e.preventDefault();
    const host = form.querySelector('input').value.trim();
    if (host) runDiag(form.dataset.kind, host);
  });
  $('#tools').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-host]');
    if (chip) { const input = chip.closest('form').querySelector('input'); input.value = chip.dataset.host; input.focus(); return; }
    const clear = e.target.closest('[data-clear]');
    if (clear) {
      const kind = clear.dataset.clear;
      $(`[data-out="${kind}"]`).innerHTML = placeholderLine(TOOL[kind].verb);
      $(`[data-sum="${kind}"]`).innerHTML = '';
      if (diagDone || diagKind !== kind) setToolPill(kind, 'Idle', '');
    }
  });

  // Explainers: one delegated handler for every "How it works" button on the page
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-why]');
    if (!b) return;
    const key = b.dataset.why;
    const panel = $(`[data-why-panel="${CSS.escape(key)}"]`);
    if (!panel) return;
    const open = panel.hidden;
    panel.hidden = !open;
    b.setAttribute('aria-expanded', String(open));
    if (open) opened.add(key); else opened.delete(key);
  });
  function syncExplainAll() {
    $('#explain-all').setAttribute('aria-pressed', String(explainAll));
    $('#explain-all').textContent = explainAll ? 'Hide explanations' : 'Explain all';
  }
  $('#explain-all').addEventListener('click', () => {
    explainAll = !explainAll;
    store.set('frExplain', explainAll ? '1' : null);
    if (!explainAll) opened.clear();
    $$('[data-why-panel]').forEach((p) => { p.hidden = !explainAll; });
    $$('[data-why]').forEach((b) => b.setAttribute('aria-expanded', String(explainAll)));
    syncExplainAll();
  });

  // Theme: light by default; dark only when chosen here
  let theme = store.get('frTheme') === 'dark' ? 'dark' : 'light';
  function applyTheme() {
    if (theme === 'dark') document.documentElement.dataset.theme = 'dark';
    else delete document.documentElement.dataset.theme;
    $('#theme').textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
  }
  $('#theme').addEventListener('click', () => {
    theme = theme === 'dark' ? 'light' : 'dark';
    store.set('frTheme', theme === 'dark' ? 'dark' : null);
    applyTheme();
  });

  // --- Clock + polling ---
  setInterval(() => {
    $('#updated').textContent = lastOk
      ? 'Updated ' + Math.round((Date.now() - lastOk) / 1000) + 's ago · refreshes every ' + POLL_MS / 1000 + 's'
      : 'Talking to the router…';
  }, 1000);
  setInterval(() => { if (!document.hidden && tab !== 'tools') loadTab(tab, true); }, POLL_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && tab !== 'tools') loadTab(tab, true); });

  applyTheme();
  buildPresets('down');
  buildPresets('up');
  syncExplainAll();
  const fromHash = location.hash.slice(1);
  selectTab(TABS.includes(fromHash) ? fromHash : store.get('frTab') || 'devices');
  if (tab !== 'devices') loadTab('devices', true);  // fetch caps + router header even if we opened elsewhere
})();
