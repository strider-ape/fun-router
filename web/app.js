(() => {
  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
  const esc = (x) => String(x ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* unavailable */ } },
  };

  const POLL_MS = 10000;
  const IFACE_COLOR = { '5': 'c-blue', '2.4': 'c-mint', lan: 'c-pink', none: 'c-grey' };
  const PRESETS = [0, 0.5, 1, 2, 5, 10];
  const LOCK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><rect x="4" y="10" width="16" height="11"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>';
  const PEN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16v4z"/><path d="M13 7l4 4"/></svg>';

  const TABS = ['devices', 'internet', 'wifi', 'security', 'tools'];
  const data = {};            // per-tab last payload
  let tab = 'devices';
  let filter = store.get('panelFilter') || 'all';
  let query = '';
  let lastOk = 0;
  let pin = store.get('panelPin') || '';
  let current = null;         // device a dialog is acting on
  let loginDismissed = false;
  let explainAll = store.get('panelExplain') === '1';
  let caps = [];

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
  const pct = (n, d) => d ? +(100 * n / d).toFixed(2) : 0;
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
  const bars = (rssi) => rssi == null ? 0 : rssi >= -55 ? 4 : rssi >= -65 ? 3 : rssi >= -75 ? 2 : 1;

  // --- Explainers ---
  function explain(id, ctx) {
    const e = window.EXPLAIN[id];
    if (!e) return '';
    const field = (f) => typeof f === 'function' ? f(ctx) : f;
    const change = field(e.change), example = field(e.example);
    const open = explainAll ? ' open' : '';
    return `<details class="explain" data-explain="${id}"${open}>
      <summary>How it works</summary>
      <div class="explain-body">
        <div><h4>What it is</h4><p>${field(e.what)}</p></div>
        ${change ? `<div><h4>${esc(e.effect || 'When you change it')}</h4><p>${change}</p></div>` : ''}
        ${example ? `<div><h4>Example</h4><p class="ex">${example}</p></div>` : ''}
      </div>
    </details>`;
  }
  const row = (dt, dd, explainId, ctx) =>
    `<div class="kv-row"><dt>${esc(dt)}</dt><dd>${dd}</dd>${explainId ? explain(explainId, ctx) : ''}</div>`;

  // --- Tabs ---
  function selectTab(name) {
    if (!TABS.includes(name)) name = 'devices';
    clearInterval(diagTimer);  // stop any ping/traceroute poll when leaving its view
    tab = name;
    store.set('panelTab', name);
    for (const t of TABS) {
      const btn = $('#tab-' + t), view = $('#view-' + t);
      const on = t === name;
      btn.setAttribute('aria-selected', String(on));
      btn.tabIndex = on ? 0 : -1;
      view.hidden = !on;
    }
    loadTab(name);
  }

  const LOADERS = {
    devices: () => api('/api/state'),
    internet: () => api('/api/internet'),
    wifi: () => api('/api/wifi'),
    security: () => api('/api/security'),
    tools: async () => ({}),
  };
  const RENDERERS = {
    devices: renderDevices, internet: renderInternet, wifi: renderWifi,
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
      if (e.kind === 'login') { banner('login'); if (!loginDismissed && !anyDialogOpen()) openLogin(); }
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
    // A restored tab may belong to a capability this router lacks; fall back to Devices.
    if ($('#tab-' + tab).hidden) selectTab('devices');
  }

  function renderEmpty(name, msg) {
    const host = { devices: '#grid', internet: '#internet', wifi: '#wifi', security: '#security', tools: '#tools' }[name];
    $(host).innerHTML = `<div class="empty box">${esc(msg)}</div>`;
  }

  // --- Router header ---
  function renderRouter(r, wan) {
    if (!r) return;
    $('#r-name').textContent = (r.model || 'Router');
    $('#r-sub').textContent = (data.devices && data.devices.family ? data.devices.family.split('(')[0].trim() : 'Router') + ' · fw ' + (r.firmware || '?');
    const up = wan ? wan.up : (r.wan && /^up/i.test(r.wan.status || ''));
    const pill = $('#r-net');
    pill.textContent = up ? 'Internet up' : 'Internet down';
    pill.className = 'net-pill ' + (up ? 'up' : 'down');
    $('#r-up').textContent = (r.uptime || '—').replace(/ days?,?/, 'd').replace(/ min/, 'm');
    $('#r-cpu').textContent = r.cpu || '—';
    $('#r-mem').textContent = r.memory || '—';
  }

  // --- Devices view ---
  function ifaceOf(d) {
    const ifaces = (data.devices && data.devices.interfaces) || [];
    return ifaces.find((i) => i.id === d.iface) || null;
  }
  function placeClass(d) {
    const i = ifaceOf(d);
    if (!i) return IFACE_COLOR.none;
    return i.kind === 'wifi' ? IFACE_COLOR[i.band] || IFACE_COLOR.none : IFACE_COLOR.lan;
  }
  const placeLabel = (d) => { const i = ifaceOf(d); return i ? i.label : (d.online ? 'Located by IP only' : 'Not connected'); };
  const nameOf = (d) => d.nickname || d.hostname || 'Unknown device';

  function deviceFilters() {
    const ifaces = (data.devices && data.devices.interfaces) || [];
    const f = [['all', 'All', () => true]];
    if (ifaces.some((i) => i.band === '5')) f.push(['5', '5 GHz', (d) => { const i = ifaceOf(d); return i && i.band === '5'; }]);
    if (ifaces.some((i) => i.band === '2.4')) f.push(['24', '2.4 GHz', (d) => { const i = ifaceOf(d); return i && i.band === '2.4'; }]);
    if (ifaces.some((i) => i.kind === 'lan')) f.push(['wired', 'Wired / AP', (d) => { const i = ifaceOf(d); return i && i.kind === 'lan'; }]);
    f.push(['blocked', 'Blocked', (d) => d.blocked]);
    f.push(['limited', 'Limited', (d) => d.limit.down || d.limit.up]);
    return f;
  }

  function renderDevices(state) {
    renderRouter(state.router);
    const devices = [...state.devices].sort((a, b) =>
      (b.online - a.online) || nameOf(a).localeCompare(nameOf(b)));
    const online = devices.filter((d) => d.online);
    const onWifi = online.filter((d) => { const i = ifaceOf(d); return i && i.kind === 'wifi'; });
    const stats = [
      ['c-yellow', online.length, 'Online now'],
      ['c-blue', onWifi.length, 'On Wi-Fi'],
      ['c-pink', online.length - onWifi.length, 'Wired / upstairs'],
      ['c-red', devices.filter((d) => d.blocked).length, 'Blocked'],
      ['c-orange', devices.filter((d) => d.limit.down || d.limit.up).length, 'Speed-limited'],
    ];
    $('#stats').innerHTML = stats.map(([c, n, l]) => `<div class="stat ${c}"><b>${n}</b><span>${l}</span></div>`).join('');

    const filters = deviceFilters();
    if (!filters.some((f) => f[0] === filter)) filter = 'all';
    $('#chips').innerHTML = filters.map(([key, label, fn]) =>
      `<button class="chip" type="button" data-filter="${key}" aria-pressed="${filter === key}">${esc(label)}<em>${devices.filter(fn).length}</em></button>`).join('');

    if (!$('#devices-explain').children.length) {
      $('#devices-explain').innerHTML = '<div class="toolbar" style="margin-bottom:16px">' +
        ['device.location', 'device.online', 'device.privateMac', 'device.block', 'device.limit']
          .map((id) => explain(id)).join('') + '</div>';
    }

    const test = (filters.find((f) => f[0] === filter) || filters[0])[2];
    const q = query.trim().toLowerCase();
    const shown = devices.filter(test).filter((d) => !q ||
      [nameOf(d), d.hostname, d.ip, d.mac].some((x) => x && x.toLowerCase().includes(q)));
    $('#grid').innerHTML = shown.length ? shown.map(card).join('') : `<div class="empty box">No devices match.</div>`;
  }

  function card(d) {
    const limited = d.limit.down || d.limit.up;
    const badges = [];
    if (d.blocked) badges.push('<span class="badge c-red">Blocked</span>');
    if (d.limit.down) badges.push(`<span class="badge c-orange">↓ max ${esc(limitText(d.limit.down))}</span>`);
    if (d.limit.up) badges.push(`<span class="badge c-orange">↑ max ${esc(limitText(d.limit.up))}</span>`);
    if (d.privateMac) badges.push('<span class="badge plain" title="Randomised MAC. If it changes, blocks stop matching.">Private MAC</span>');
    if (d.wifi && d.wifi.uptime != null) badges.push(`<span class="badge plain">On for ${esc(duration(d.wifi.uptime))}</span>`);

    let live;
    if (d.wifi) {
      const n = bars(d.wifi.rssi);
      live = `<div class="live">
        <span class="meter" title="Download right now">↓ ${esc(kbps(d.wifi.downKbps))}</span>
        <span class="meter" title="Upload right now">↑ ${esc(kbps(d.wifi.upKbps))}</span>
        <span class="signal" title="Signal ${esc(d.wifi.rssi)} dBm" aria-label="Signal ${n} of 4">${[1, 2, 3, 4].map((i) => `<i class="${i <= n ? 'on' : ''}"></i>`).join('')}</span>
        <span class="rssi">${esc(d.wifi.rssi)} dBm</span>
      </div>`;
    } else if (d.online && ifaceOf(d) && ifaceOf(d).kind === 'lan') {
      live = '<p class="live-note">Through a cable or an access point on this port. Live per-device speed only shows for the router\'s own Wi-Fi.</p>';
    } else {
      live = d.online ? '' : '<p class="live-note">Not seen recently. It still holds an address lease.</p>';
    }

    const actions = d.protected
      ? `<p class="protected">${LOCK_ICON}<span>${esc(d.protected)}</span></p>`
      : `${caps.includes('block') ? `<button class="btn ${d.blocked ? 'btn-go' : 'btn-stop'}" type="button" data-act="${d.blocked ? 'unblock' : 'block'}">${d.blocked ? 'Unblock' : 'Block'}</button>` : ''}
         ${caps.includes('limit') ? `<button class="btn" type="button" data-act="limit" ${d.ip ? '' : 'disabled'}>${limited ? 'Edit limit' : 'Limit speed'}</button>` : ''}`;

    return `<article class="card ${d.blocked ? 'is-blocked' : ''} ${d.online ? '' : 'is-offline'}" data-mac="${esc(d.mac)}">
      <div class="card-head ${placeClass(d)}"><span>${esc(placeLabel(d))}</span><span class="state-dot ${d.online ? 'on' : ''}">${d.online ? 'Online' : 'Offline'}</span></div>
      <div class="card-body">
        <div class="name-row"><h3>${esc(nameOf(d))}</h3><button class="icon-btn" type="button" data-act="rename" aria-label="Rename ${esc(nameOf(d))}">${PEN_ICON}</button></div>
        ${d.nickname && d.hostname ? `<p class="sub">${esc(d.hostname)}</p>` : ''}
        <dl class="facts mono"><dt>IP</dt><dd>${esc(d.ip || '—')}</dd><dt>MAC</dt><dd>${esc(d.mac)}</dd></dl>
        ${badges.length ? `<div class="badges">${badges.join('')}</div>` : ''}
        ${live}
      </div>
      ${actions.trim() ? `<div class="card-actions">${actions}</div>` : ''}
    </article>`;
  }

  // --- Internet view ---
  function panel(title, tag, bodyHtml, wide) {
    return `<section class="panel${wide ? ' wide' : ''} box">
      <div class="panel-head c-grey"><h3>${esc(title)}</h3>${tag ? `<span class="tag">${esc(tag)}</span>` : ''}</div>
      <div class="panel-body">${bodyHtml}</div></section>`;
  }
  const flag = (text, cls) => `<span class="flag ${cls || 'c-grey'}">${esc(text)}</span>`;

  function renderInternet(info) {
    renderRouter(info.router, info.wan);
    const wan = info.wan || {}, f = info.fibre, v6 = info.ipv6 || {};
    const kindFlag = wan.addressKind === 'public' ? flag('public', 'c-mint')
      : wan.addressKind === 'cgnat' ? flag('CGNAT', 'c-orange')
      : wan.addressKind === 'private' ? flag('private', 'c-orange') : '';

    const conn = `<dl class="kv">
      ${row('Status', (wan.up ? 'Connected' : 'Down') + (wan.upSeconds ? ' · ' + duration(wan.upSeconds) : ''), 'wan.session', { vlan: wan.vlan, iface: wan.iface, up: duration(wan.upSeconds), gateway: wan.gateway })}
      ${row('Type', esc((wan.protocol || '') + (wan.type ? ' · ' + wan.type : '')))}
      ${row('Router uptime', duration(info.uptimeSeconds))}
    </dl>`;

    const addr = `<dl class="kv">
      ${row('IPv4 (WAN)', esc(wan.ip || '—') + ' ' + kindFlag, 'wan.address', { ip: wan.ip, kind: wan.addressKind })}
      ${row('Gateway', esc(wan.gateway || '—'))}
      ${row('IPv6 prefix', esc(v6.prefix || '—') + (v6.wan && v6.wan.ip ? ' ' + flag('up', 'c-mint') : ''), 'wan.ipv6', { prefix: v6.prefix, example: v6.lanAddress })}
      ${row('DNS', esc((info.dns || []).slice(0, 2).join(', ') || '—'), 'wan.dns', { dns: (info.dns || []).join(', ') })}
    </dl>`;

    let fibrePanel = '';
    if (f) {
      const lo = -30, hi = -8, span = hi - lo;
      const posRx = Math.max(0, Math.min(100, (f.rxDbm - lo) / span * 100));
      const good = f.rxDbm > -25, warn = f.rxDbm <= -25 && f.rxDbm > -27;
      const gauge = `<div>
        <div class="gauge" role="img" aria-label="Receive power ${esc(f.rxDbm)} dBm">
          <span class="c-red" style="width:${(-27 - lo) / span * 100}%"></span>
          <span class="c-orange" style="width:${2 / span * 100}%"></span>
          <span class="c-mint" style="width:${(hi - -25) / span * 100}%"></span>
          <i class="needle" style="left:${posRx}%"></i>
        </div>
        <div class="gauge-scale"><span>-30</span><span>-27</span><span>-25</span><span>-8 dBm</span></div>
      </div>`;
      fibrePanel = panel('Fibre (GPON)', f.onuState === 'O5' ? 'operating' : f.onuState, `
        <p class="big-number">${esc(f.rxDbm != null ? f.rxDbm.toFixed(1) : '—')}<small>dBm received ${good ? '· healthy' : warn ? '· near limit' : '· low'}</small></p>
        ${gauge}
        <dl class="kv">
          ${row('Receive power', esc(f.rxDbm != null ? f.rxDbm.toFixed(2) : '—') + ' dBm', 'fibre.rx', { rx: f.rxDbm })}
          ${row('Transmit power', esc(f.txDbm != null ? f.txDbm.toFixed(2) : '—') + ' dBm', 'fibre.tx', { tx: f.txDbm, bias: f.biasMa })}
          ${row('ONU state', esc(f.onuState || '—'), 'fibre.onu', { state: f.onuState })}
          ${row('Temperature', esc(f.temperatureC != null ? f.temperatureC.toFixed(1) + ' °C' : '—'))}
          ${row('FEC / HEC errors', esc(f.fecErrors) + ' / ' + esc(f.hecErrors), 'fibre.errors', { fec: f.fecErrors, hec: f.hecErrors })}
        </dl>`);
    }

    const days = info.uptimeSeconds ? info.uptimeSeconds / 86400 : 0;
    const perDay = f && f.bytesIn && days ? bytes(f.bytesIn / days) : null;
    const usage = f ? panel('Data used', 'since reboot', `
      <p class="big-number">${esc(bytes(f.bytesIn))}<small>downloaded</small></p>
      <dl class="kv">
        ${row('Downloaded', bytes(f.bytesIn), 'usage', { perDay, days: Math.round(days) })}
        ${row('Uploaded', bytes(f.bytesOut))}
      </dl>`) : '';

    const slow = (info.ports || []).find((p) => p.up && /100/.test(p.speed || ''));
    const ports = panel('LAN ports', null, `<dl class="kv">
      ${(info.ports || []).map((p) => row(p.name, p.up ? esc(p.speed + ' ' + (p.duplex || '')) + (/100/.test(p.speed || '') ? ' ' + flag('100M', 'c-orange') : ' ' + flag('gigabit', 'c-mint')) : '<span class="dim">not connected</span>')).join('')}
      <div class="kv-row"><dt>About</dt><dd></dd>${explain('lan.ports', { slow: slow && slow.name })}</div>
    </dl>`);

    $('#internet').innerHTML = panel('Connection', wan.up ? 'up' : 'down', conn) + panel('Addresses', null, addr) + (fibrePanel || '') + (usage || '') + ports;
  }

  // --- Wi-Fi view ---
  function channelMap(band, radios, neighbours) {
    const nets = [
      ...radios.filter((r) => r.band === band && r.channel).map((r) => ({ ch: r.channel, w: r.widthMhz || 20, sig: 90, cls: 'mine', label: r.ssid })),
      ...neighbours.filter((n) => (band === '2.4' ? n.channel <= 14 : n.channel > 14) && n.channel).map((n) => ({ ch: n.channel, w: n.widthMhz || 20, sig: n.signal || 10, cls: n.yours ? 'yours' : 'other', label: n.ssid })),
    ];
    if (!nets.length) return '';
    const chans = band === '2.4' ? [1, 6, 11] : [36, 44, 52, 60, 100, 108, 116, 124, 132, 140, 149, 157, 165];
    const W = 620, H = 150, padL = 8, padB = 22, top = 10;
    const lo = band === '2.4' ? 0 : 32, hi = band === '2.4' ? 15 : 169;
    const x = (ch) => padL + (ch - lo) / (hi - lo) * (W - padL * 2);
    const bw = (w) => w / 5 / (hi - lo) * (W - padL * 2);
    const bars = nets.sort((a, b) => a.sig - b.sig).map((n) => {
      const h = Math.max(6, (n.sig / 100) * (H - top - padB));
      const w = Math.max(10, bw(n.w));
      return `<rect class="net ${n.cls}" x="${(x(n.ch) - w / 2).toFixed(1)}" y="${(H - padB - h).toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" rx="3"><title>${esc(n.label || '?')} · ch ${n.ch} · ${n.w}MHz${n.cls !== 'other' ? ' · yours' : ' · ' + n.sig + '%'}</title></rect>`;
    }).join('');
    const ticks = chans.map((ch) => `<text class="tick" x="${x(ch).toFixed(1)}" y="${H - 6}" text-anchor="middle">${ch}</text>`).join('');
    return `<svg class="chanmap" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(band)} GHz channel usage">
      <line class="axis" x1="${padL}" y1="${H - padB}" x2="${W - padL}" y2="${H - padB}"/>${bars}${ticks}</svg>
      <div class="legend"><span><i class="c-yellow"></i>This router</span><span><i class="c-pink"></i>Your other APs</span><span><i class="c-grey"></i>Neighbours</span></div>`;
  }

  function renderWifi(info) {
    const radios = info.radios || [], neighbours = info.neighbours || [];
    const panels = radios.map((r) => {
      const overlaps = neighbours.filter((n) => n.channel && Math.abs(n.channel - r.channel) < (r.band === '2.4' ? 5 : 4) && !n.yours).length;
      const secFlag = /WPA3/.test(r.security) ? flag('WPA3', 'c-mint') : /WPA2/.test(r.security) ? flag('WPA2', 'c-mint') : flag(r.security, 'c-red');
      const wpsFlag = r.wps.enabled ? flag(r.wps.defaultPin ? 'WPS · default PIN' : 'WPS on', 'c-red') : flag('WPS off', 'c-mint');
      return panel(`${r.band} GHz · ${r.ssid || 'Wi-Fi'}`, r.enabled ? r.generation : 'disabled', `
        <dl class="kv">
          ${row('Channel', esc(r.channel) + (r.autoChannel ? ' ' + flag('auto', 'c-blue') : '') + ' · ' + esc(r.widthMhz) + ' MHz', 'wifi.channel', { band: r.band, channel: r.channel, width: r.widthMhz, overlaps })}
          ${row('Width', esc(r.widthMhz) + ' MHz', 'wifi.width', { band: r.band, width: r.widthMhz, sideband: r.sideband })}
          ${row('Standard', esc(r.standard || '—'), 'wifi.standard', { standard: r.standard, generation: r.generation })}
          ${row('Transmit power', esc(r.powerPercent) + '%', 'wifi.power', { power: r.powerPercent })}
          ${row('Security', esc(r.security) + ' ' + secFlag, 'wifi.security', { security: r.security, cipher: r.cipher })}
          ${row('Mgmt frames (PMF)', esc(r.pmf), 'wifi.pmf', { pmf: r.pmf })}
          ${row('WPS', r.wps.enabled ? 'On' + (r.wps.defaultPin ? ' · factory PIN' : '') + ' ' + wpsFlag : 'Off ' + wpsFlag, 'wifi.wps', { enabled: r.wps.enabled, pin: r.wps.pin, defaultPin: r.wps.defaultPin })}
          ${row('Clients', esc(r.clients) + ' connected')}
          ${row('Guest SSIDs', esc(r.guestNetworks.used) + ' of ' + esc(r.guestNetworks.slots) + ' used', 'wifi.guest', r.guestNetworks)}
        </dl>`);
    }).join('');

    const busiest = (() => {
      const byCh = {};
      neighbours.forEach((n) => { if (n.channel) byCh[n.channel] = (byCh[n.channel] || 0) + 1; });
      const top = Object.entries(byCh).sort((a, b) => b[1] - a[1])[0];
      return top ? +top[0] : null;
    })();
    const maps = ['2.4', '5'].filter((b) => radios.some((r) => r.band === b)).map((b) =>
      `<div><p class="lede">${b} GHz</p>${channelMap(b, radios, neighbours) || '<p class="live-note">No data.</p>'}</div>`).join('');
    const nb = panel('Nearby networks & channels', neighbours.length + ' seen', `
      ${maps}
      <div class="kv-row"><dt>Reading it</dt><dd></dd>${explain('wifi.neighbours', { busiest })}</div>`, true);

    $('#wifi').innerHTML = panels + nb;
  }

  // --- Security view ---
  function renderSecurity(info) {
    const checks = info.checks || [];
    const counts = { bad: 0, warn: 0, info: 0, good: 0 };
    checks.forEach((c) => counts[c.level]++);
    $('#sec-badge').hidden = false;
    $('#sec-badge').textContent = counts.bad || counts.warn || '✓';
    $('#sec-badge').className = 'tab-badge' + (counts.bad || counts.warn ? '' : ' ok');

    const score = `<div class="score">
      <div class="stat c-red"><b>${counts.bad}</b><span>Act now</span></div>
      <div class="stat c-orange"><b>${counts.warn}</b><span>Look into</span></div>
      <div class="stat c-blue"><b>${counts.info}</b><span>Good to know</span></div>
      <div class="stat c-mint"><b>${counts.good}</b><span>All good</span></div>
    </div>`;
    const LEVEL = { bad: 'Act', warn: 'Check', info: 'Info', good: 'Good' };
    const list = checks.map((c) => `<article class="check ${c.level}">
      <div class="check-level">${LEVEL[c.level]}</div>
      <div class="check-main"><h3>${esc(c.title)}</h3><p class="check-detail">${esc(c.detail)}</p></div>
    </article>`).join('');
    $('#security').innerHTML = `<div class="section-head"><div><h2>Security check-up</h2><p>Read-only. Nothing here is changed.</p></div></div>
      <div style="margin-bottom:18px">${explain('sec.overview')}</div>${score}<div class="checks">${list}</div>`;
  }

  // --- Tools view ---
  let diagTimer = null;
  function renderTools() {
    if ($('#tools').children.length) return;
    const forms = [];
    if (caps.includes('ping')) forms.push(toolPanel('ping', 'Ping', 'Send pings from the router to any host', 'tool.ping'));
    if (caps.includes('traceroute')) forms.push(toolPanel('traceroute', 'Traceroute', 'Trace the path from the router to a host', 'tool.traceroute'));
    $('#tools').innerHTML = forms.join('') || '<div class="empty box">This router has no diagnostics.</div>';
  }
  function toolPanel(kind, title, sub, explainId) {
    return panel(title, 'from the router', `
      <p class="lede">${esc(sub)}</p>
      <form class="tool-form" data-kind="${kind}">
        <input type="text" name="host" placeholder="1.1.1.1 or google.com" autocomplete="off" inputmode="url" aria-label="${esc(title)} host">
        <button class="btn btn-main" type="submit">${esc(title)}</button>
      </form>
      ${explain(explainId)}
      <div data-out="${kind}"></div>`, true);
  }

  async function runDiag(kind, host) {
    const out = $(`[data-out="${kind}"]`);
    out.innerHTML = `<pre class="term">Starting ${esc(kind)} to ${esc(host)}…</pre>`;
    try {
      await api('/api/diag', { kind, host });
    } catch (e) {
      if (e.kind === 'login') { banner('login'); openLogin(); } else out.innerHTML = `<pre class="term">${esc(e.message)}</pre>`;
      return;
    }
    clearInterval(diagTimer);
    const poll = async () => {
      let r;
      try { r = await api('/api/diag'); } catch (e) { clearInterval(diagTimer); return; }
      if (r.kind !== kind) return;
      out.innerHTML = kind === 'traceroute' ? traceroute(r) : pingOut(r);
      if (r.done) clearInterval(diagTimer);
    };
    diagTimer = setInterval(poll, 1000);
    poll();
  }

  function pingOut(r) {
    const lines = r.lines || [];
    const stat = lines.find((l) => /packet loss/.test(l)) || '';
    const loss = (stat.match(/(\d+)% packet loss/) || [])[1];
    const rtt = (lines.find((l) => /min\/avg\/max/.test(l)) || '').match(/=\s*([\d.]+)\/([\d.]+)\/([\d.]+)/);
    const times = lines.map((l) => (l.match(/time=([\d.]+)/) || [])[1]).filter(Boolean).map(Number);
    let summary = '';
    if (rtt || loss != null) {
      summary = `<dl class="ping-sum">
        <div><dt>Min</dt><dd>${rtt ? rtt[1] : '—'}</dd></div>
        <div><dt>Avg</dt><dd>${rtt ? rtt[2] : (times.length ? (times.reduce((a, b) => a + b, 0) / times.length).toFixed(1) : '—')}</dd></div>
        <div><dt>Max</dt><dd>${rtt ? rtt[3] : '—'}</dd></div>
        <div><dt>Loss</dt><dd>${loss != null ? loss + '%' : '—'}</dd></div>
      </dl>`;
    }
    return summary + `<pre class="term">${esc(lines.join('\n') || 'Waiting for replies…')}${r.done ? '' : '\n…'}</pre>`;
  }

  function traceroute(r) {
    const lines = r.lines || [];
    const hops = lines.map((l) => l.match(/^\s*(\d+)\s+(.*?)\s+([\d.]+)\s*ms/)).filter(Boolean);
    const max = Math.max(1, ...hops.map((h) => +h[3]));
    const list = hops.map((h) => `<div class="hop"><b>${esc(h[1])}</b><span class="host" title="${esc(h[2])}">${esc(h[2])}</span><span class="ms">${esc(h[3])} ms</span>
      <span style="grid-column:2/4"><span class="bar" style="width:${Math.max(4, +h[3] / max * 100)}%"></span></span></div>`).join('');
    return `<div class="hops">${list || '<p class="live-note">Tracing…</p>'}</div><pre class="term dim">${esc(lines.join('\n'))}${r.done ? '' : '\n…'}</pre>`;
  }

  // --- Banner / toast ---
  function banner(kind, msg) {
    const el = $('#banner');
    if (!kind) { el.classList.remove('show'); return; }
    $('#banner-text').textContent = kind === 'login'
      ? 'The router logged this computer out, so the panel can\'t read it right now.' : msg;
    const btn = $('#banner-btn');
    btn.textContent = kind === 'login' ? 'Log in' : 'Try again';
    btn.onclick = kind === 'login' ? openLogin : () => loadTab(tab);
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

  // --- Actions on devices ---
  async function run(button, path, body, after) {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Working…';
    try {
      const res = await api(path, body);
      if (after) after();
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
      button.textContent = label;
    }
  }

  function findDevice(mac) { return (data.devices && data.devices.devices || []).find((d) => d.mac === mac); }

  function openConfirm(d, act) {
    current = d;
    const block = act === 'block';
    $('#confirm-head').className = 'dlg-head ' + (block ? 'c-red' : 'c-mint');
    $('#confirm-title').textContent = (block ? 'Block ' : 'Unblock ') + nameOf(d) + '?';
    $('#confirm-text').textContent = block
      ? 'It loses internet until you unblock it. It stays on the Wi-Fi and the local network.'
      : 'Internet access comes back right away.';
    const ok = $('#confirm-ok');
    ok.textContent = block ? 'Block it' : 'Unblock';
    ok.className = 'btn ' + (block ? 'btn-stop' : 'btn-go');
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
      `<input type="number" min="0.256" step="0.1" placeholder="Custom" aria-label="Custom ${dir} limit Mbps"><span class="unit">Mbps</span>`;
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
  $('#limit-apply').addEventListener('click', async (e) => {
    const down = Math.round(limitChoice.down * 1000), up = Math.round(limitChoice.up * 1000);
    if (!down && !up) { toast('Pick a download or upload limit, or use Remove limit.', true); return; }
    const min = data.devices.minLimitKbps;
    if ((down && down < min) || (up && up < min)) { toast(`Limits must be at least ${min} kbps.`, true); return; }
    if (await run(e.currentTarget, '/api/limit', { mac: current.mac, down, up })) $('#dlg-limit').close();
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
    if (e.submitter && e.submitter.value === 'cancel') return;
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
  $('#login-form').addEventListener('submit', async (e) => {
    if (e.submitter && e.submitter.value === 'cancel') { loginDismissed = true; return; }
    e.preventDefault();
    const ok = await run($('#login-go'), '/api/login', { username: $('#login-user').value, password: $('#login-pass').value });
    $('#login-pass').value = '';
    if (ok) { $('#dlg-login').close(); loginDismissed = false; toast('Logged in to the router'); loadTab(tab); }
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
  $('#chips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    filter = b.dataset.filter;
    store.set('panelFilter', filter);
    if (data.devices) renderDevices(data.devices);
  });
  $('#q').addEventListener('input', (e) => { query = e.target.value; if (data.devices) renderDevices(data.devices); });
  $('#refresh').addEventListener('click', () => loadTab(tab));

  $('.tabs').addEventListener('click', (e) => { const t = e.target.closest('[data-tab]'); if (t) selectTab(t.dataset.tab); });
  $('.tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const vis = TABS.filter((t) => !$('#tab-' + t).hidden);
    const i = vis.indexOf(tab);
    const next = vis[(i + (e.key === 'ArrowRight' ? 1 : vis.length - 1)) % vis.length];
    selectTab(next); $('#tab-' + next).focus();
  });

  $('#tools').addEventListener('submit', (e) => {
    const form = e.target.closest('[data-kind]');
    if (!form) return;
    e.preventDefault();
    const host = form.querySelector('input').value.trim();
    if (host) runDiag(form.dataset.kind, host);
  });

  // Explain-all toggle
  function syncExplainAll() {
    $('#explain-all').setAttribute('aria-pressed', String(explainAll));
    $('#explain-all').textContent = explainAll ? 'Hide help' : 'Explain all';
  }
  $('#explain-all').addEventListener('click', () => {
    explainAll = !explainAll;
    store.set('panelExplain', explainAll ? '1' : null);
    $$('details.explain').forEach((d) => { d.open = explainAll; });
    syncExplainAll();
  });

  // Theme
  const savedTheme = store.get('panelTheme');
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.theme = dark ? 'light' : 'dark';
    store.set('panelTheme', document.documentElement.dataset.theme);
  });

  // --- Clock + polling ---
  setInterval(() => {
    $('#updated').textContent = lastOk
      ? 'Updated ' + Math.round((Date.now() - lastOk) / 1000) + 's ago · refreshes every ' + POLL_MS / 1000 + 's'
      : 'Talking to the router…';
  }, 1000);
  setInterval(() => { if (!document.hidden && tab !== 'tools') loadTab(tab, true); }, POLL_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && tab !== 'tools') loadTab(tab, true); });

  buildPresets('down');
  buildPresets('up');
  syncExplainAll();
  selectTab(store.get('panelTab') || 'devices');
  if (tab !== 'devices') loadTab('devices', true);  // fetch caps + router header even if we opened elsewhere
})();
