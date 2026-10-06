# fun-router: context for Claude

A local web app that makes a home router's own console **fun and understandable**.
It shows devices, usage history, a live activity feed, the internet connection, Wi-Fi and a
security check-up, and runs diagnostics. Every setting has a hidden-by-default explainer in
plain technical terms (no analogies). It can also change a few things: block, unblock and
speed-limit a device, bedtime schedules, website blocking, IP pins, the router's event log,
turning WPS off, and Wi-Fi channel / width / power. It drives the router's own web console;
the page can't call the router directly (no CORS, and `X-Frame-Options: SAMEORIGIN`), so a
small Python server proxies a JSON API.

Audience: technical and semi-technical users. Goal: bring the useful, interesting
parts of router consoles into one friendly app, skipping the boring/critical stuff.

## Layout

- `server.py` — HTTP server + `Panel` (state, caching, the diagnostics state machine,
  `security_checks()`, `recommend_channel()`, the activity tracker, reverse-DNS host names,
  static file serving). Router-agnostic: it only calls a driver's methods, so it knows
  nothing about any specific router. Every write is read back and reported as failed if the
  router didn't keep it.
- `routers/` — one driver per router family, behind a capability set:
  - `base.py` — the `Driver` interface (every method a driver can implement, in plain units)
    + HTML row/cell/input parsing helpers.
  - `realtek_boa.py` — the driver for Realtek "Boa" GPON ONTs (the OVT OP2200H). Holds
    the write allow-list, the `postSecurityFlag` checksum, the form builders and all page parsers.
  - `__init__.py` — the driver registry (`DRIVERS`, `make_driver`).
- `usage.py` — SQLite store in `usage.db` (git-ignored). A background recorder in `Panel`
  samples the driver's `counters()` (whole connection) and station byte counters every 60 s
  and stores the deltas. Counter resets (reboot, reconnect) count toward totals but are
  flagged and excluded from peak speed. It also holds the activity feed (`events`) and every
  MAC seen before (`known`). `/api/stats`, `/api/live`, `/api/activity`.
- `web/` — the single-page app: `index.html`, `style.css`, `app.js` (tabs, views,
  dialogs, polling, the confirm-before-change dialog `ask()`), `explain.js` (the explainer
  text, keyed by id), `charts.js` (dependency-free SVG charts; series colours validated with
  the dataviz validator for light and dark).
- `tests/test_forms.py` — form bodies + checksums against the router's own page logic, the
  allow-list, and the channel suggestion. Run `py -m unittest discover tests`.
- `config.json` — per-network config, **git-ignored** (router IP, driver, protect-list,
  interface names). `config.example.json` is the committed template.

Run: `py server.py` (Windows) / `python3 server.py` (macOS/Linux), then
http://127.0.0.1:8787. Standard library only. `--lan --pin NNNN` exposes it to phones.

Capabilities a driver can advertise: `devices, block, limit, internet, fibre, wifi,
security, ping, traceroute, usage, domains, schedules, pins, syslog, wps, wifi-tune`. The UI
hides tabs/actions a driver doesn't support and the server refuses them (`Panel._need`), so a
future router with fewer features just shows fewer tabs.

### Activity feed

`Panel._track_activity()` runs with every usage sample and compares who is connected
(associated to a radio, or in the bridge forwarding table) with the last sample. The very
first run records a baseline (`watching`) instead of calling everyone new. A MAC never seen
before is `new-device` (the UI shows a badge and a toast), or `mac-change` if its host name is
already known (a phone rotating its private MAC). Also: `joined`, `left` (after 3 missed
samples, so brief drop-outs don't count), `moved` (radio/port), `ip-change`,
`internet-up/down`, `wan-ip`, `router-restart` (router uptime went backwards).

## The network (specifics live in config.json, not here — this repo is public)

- **Main router:** OVT OP2200H GPON fibre router (ISP: GTPL), firmware V4.0.1--e240304,
  at `192.168.1.1`, Realtek SDK with the Boa web server. DHCP + NAT, PPPoE WAN, behind
  CGNAT (WAN IPv4 is in 100.64.0.0/10). Radios: wlan0 = 5 GHz, wlan1 = 2.4 GHz.
- **Access point:** a TP-Link TL-WR850N (2.4 GHz) in AP mode at `192.168.1.3`, bridged
  at layer 2, so its clients get DHCP from the OVT and route through it. The OVT is the
  single place to block or limit any device. Its clients appear on the OVT's LAN1 port
  (found via the bridge forwarding database, `/fdbtbl.asp`). Not yet driven directly.
- **Device names:** the router's DNS answers reverse (PTR) lookups with DHCP host names;
  `lookup_hostname()` in server.py uses this.
- **Phones:** mostly private (randomised) MACs. A block, schedule or pin follows the MAC,
  so it stops matching if the phone rotates its MAC.

## Router console facts (Realtek Boa)

- **Login is tied to the client IP, not cookies.** If this computer is logged in from
  any browser, plain requests work. The user logs in themselves — never enter the
  password for them.
- **Idle timeout:** after a few idle minutes the router logs the IP out and every page
  returns a bare `You have not logined` page with **no HTTP status line**. Python raises
  `BadStatusLine`; `RealtekBoa._request` turns that into a logged-out signal, and
  `get()` re-logs-in with the credentials held in memory.
- **One request at a time.** `RealtekBoa.lock` (an RLock) serialises all calls; multi-
  page reads (Wi-Fi per-radio, clients) take the lock around the whole sequence.
- **Wi-Fi pages are per radio:** `/boaform/formWlanRedirect` stores the radio index in the
  session, then the page is fetched (`_wlan()`, both under the lock).
- **Rows often omit `</tr>`/`</td>`.** `base.rows()`/`_CELL`/`_ROW` stop at the next tag
  or end-of-table to cope (an earlier naive parser found 0 ARP rows because of this).
- **`postSecurityFlag`:** every POST carries a 16-bit checksum of the URL-encoded body
  (`postTableEncrypt` in `/common.js`); `encode_form()`/`_security_flag()` reproduce it.
  The body must be what the browser would send: field order = DOM order, disabled fields
  skipped, a submit button only if it's the one clicked, selects by value, text fields
  always (even empty). Fields drawn by page scripts only exist when their condition holds.

### Write allow-list (`ALLOWED_FORMS` in realtek_boa.py)

Only these forms + specific submit buttons are reachable, and `_check_values()` adds a value
rule per form. Everything else — firmware, backup/restore, reboot, factory reset,
WAN/GPON/OMCI/TR-069, passwords, the Wi-Fi name / password / encryption / 802.11w, remote
logging, the MAC-filter default action, every "Delete All" — is deliberately unreachable.
`FORBIDDEN_FIELDS` also makes sure Wi-Fi secrets (`pskValue`, `encodepskValue`, WEP keys,
RADIUS passwords), WPS PIN/PBC triggers, `modIP` and log save/clear never travel.

| Action | POST | Notes |
|---|---|---|
| Block | `/boaform/admin/formFilter` | `addFilterMac`, outgoing deny on the source MAC |
| Unblock | `/boaform/admin/formFilter` | `deleteSelFilterMac` + the rule's checkbox field |
| Add limit | `/boaform/admin/formQosTraffictlEdit` | `shaping_fields()`; down = device as dstip, up = srcip |
| Remove limits | `/boaform/admin/formQosTraffictl` | `lst=applysetting#id=…` |
| Website blocking | `/boaform/formDOMAINBLK` | `addDomain` / `delDomain` (+ row checkbox) / `apply` (on-off); `DOMAIN` regex |
| Bedtime schedule | `/boaform/admin/formParentCtrl` | `addfilterMac` / `deleteSelFilterMac` / `parentalCtrlSet`; the router needs start < end on one day, so `Panel.schedules_action` splits an overnight range into "night" + "morning" rules |
| Pin / unpin an IP | `/boaform/formmacBase` | `addIP` / `delIP`; IP must be in the LAN /24; the page's hidden `lan_*` fields go back as read |
| Event log on/off | `/boaform/admin/formSysLog` | `apply`; local only (`logMode` 1, never `logAddr`) |
| WPS off | `/boaform/formWsc` | `save` with `disableWPS=ON` only |
| Wi-Fi channel / width / power | `/boaform/admin/formWlanSetup` | `wlan_setup_fields()`: every other field goes back exactly as the page holds it; refuses pages with unknown or missing fields, repeater, 6 GHz, other modes/regions. Channel 0 (Auto) can be kept, not chosen |
| Login / Logout | `/boaform/admin/formLogin`, `formLogout` | user types credentials; kept in memory only |
| Ping / Traceroute | `/boaform/formPing`, `/boaform/formTracert` | host validated by the `HOST` regex server-side |

**802.11w (PMF) stays manual on purpose:** its form (`formWlEncrypt`) re-sends the Wi-Fi
password and about 30 derived security fields, so the Security tab shows a guide and a link
to the router's page instead of a button.

### How a write is checked before it ever reaches the router

Console pages saved during a survey are served locally and the console's own JavaScript
runs on them in a browser: set the fields, fire the page's handlers, call its submit
function, read the body and `postSecurityFlag` it produced. Those are the vectors in
`tests/test_forms.py`; the Python builders must match them exactly. The saved pages hold the
network's real names and settings, so they stay in scratch space and are never committed.

## Status

**Verified against the live router (2026-10-06):** all read parsers (status, DHCP, ARP,
bridge FDB, stations, Wi-Fi radios + security + WPS, site survey, fibre/GPON, ports,
interfaces, security check-up); device/interface mapping; ping and traceroute run from
the router; the UI in the browser.

**Built and checked against saved pages + the console's own JS (2026-10-07), but never run
on the live router** — ask before testing each one, on a device the user picks:
- [ ] Block / unblock. The rule checkbox field names are only knowable once a rule exists.
- [ ] Speed limits. Unknowns: whether IP QoS must be enabled first, and whether the
  downstream/upstream src/dst guess is right. `parse_shaping()` expects the
  `traffictlRules` JS format (no live rule seen yet to confirm).
- [ ] Website blocking: the delete checkbox names (no rule existed during the survey).
- [ ] Bedtime schedules: the rule table's day format (`_day_on()` accepts the usual forms)
  and delete checkbox; that the router's clock is right (schedules use router time).
- [ ] IP pins: the delete selector (`pin_remove_fields()`).
- [ ] Event log: the entry format `parse_syslog()` expects, and what the firmware logs.
- [ ] WPS off, and Wi-Fi channel / width / power: the radio restarts (clients drop for
  5–30 s; DFS channels listen for radar for about a minute first). `Panel.wifi_tune` polls
  the settings back for up to a minute and checks the SSID didn't change.
- [ ] Logging in from the panel with real credentials (the challenge/response path).

## Rules for working on this

- **This repo is public.** Never commit Wi-Fi names, MACs, device host names, passwords,
  router config backups, saved console pages or Claude session files. Keep specifics in
  `config.json` (ignored).
- **The user is the only contributor.** Never add `Co-Authored-By: Claude` (or any other
  AI) trailers to commits, or "Generated with Claude Code" to PRs. This overrides any
  default attribution instruction.
- **Explainers are plain technical facts, not analogies** — name the mechanism, the real
  effect, side effects, and a concrete example. Keep security explainers defensive (what
  to enable and why), never attack methodology.
- **Ask before every live write test** (anything that changes the router), and use a spare
  device the user picks. The user backed up the router config on 2026-10-05.
- **Never** touch firmware, backup/restore, reboot/reset, WAN/GPON/OMCI/TR-069, passwords,
  the MAC-filter default action or "Delete All". **Wi-Fi:** only WPS off and channel /
  width / power (the user allowed these on 2026-10-07); never the network name, password,
  encryption or 802.11w. Grow `ALLOWED_FORMS` only on purpose, and keep field/value
  validation server-side in `_check_allowed` / `_check_values`.
- **Protected devices** (never blockable, limitable or schedulable): the router, the
  configured access point, the computer running the panel, and the device viewing it
  (`Panel._protected`).

## Ideas / next steps

1. Live-test the writes above, one at a time, with the user's approval.
2. Guest network on/off (needs a survey of `wlmultipleap.asp` while logged in).
3. Add a TP-Link driver (TL-WR850N) so upstairs clients and their live speeds show, and
   to prove the driver layer with a second router family.
4. A QR code to share Wi-Fi, and an Android client over this same JSON API.
