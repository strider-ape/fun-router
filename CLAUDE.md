# fun-router: context for Claude

A local web app that makes a home router's own console **fun and understandable**.
It shows devices, the internet connection, Wi-Fi and a security check-up, and runs
diagnostics — and every setting has a hidden-by-default explainer in plain technical
terms (no analogies). It can also block, unblock and speed-limit a device. It drives
the router's own web console; the page can't call the router directly (no CORS, and
`X-Frame-Options: SAMEORIGIN`), so a small Python server proxies a JSON API.

Audience: technical and semi-technical users. Goal: bring the useful, interesting
parts of router consoles into one friendly app, skipping the boring/critical stuff.

## Layout

- `server.py` — HTTP server + `Panel` (state, caching, the diagnostics state machine,
  `security_checks()`, reverse-DNS host names, static file serving). Router-agnostic:
  it only calls a driver's methods, so it knows nothing about any specific router.
- `routers/` — one driver per router family, behind a capability set:
  - `base.py` — the `Driver` interface + HTML row/cell/input parsing helpers.
  - `realtek_boa.py` — the driver for Realtek "Boa" GPON ONTs (the OVT OP2200H). Holds
    the write allow-list, the `postSecurityFlag` checksum, and all page parsers.
  - `__init__.py` — the driver registry (`DRIVERS`, `make_driver`).
- `usage.py` — usage history. A background recorder in `Panel` samples the driver's
  `counters()` (whole connection) and station byte counters every 60 s and stores the
  deltas in `usage.db` (SQLite, git-ignored). Counter resets (reboot, reconnect) count toward
  totals but are flagged and excluded from peak speed. `/api/stats` and `/api/live`.
- `web/` — the single-page app: `index.html`, `style.css`, `app.js` (tabs, views,
  dialogs, polling), `explain.js` (the explainer text, keyed by id), `charts.js` (dependency-free
  SVG charts; series colours validated with the dataviz validator for light and dark).
- `config.json` — per-network config, **git-ignored** (router IP, driver, protect-list,
  interface names). `config.example.json` is the committed template.

Run: `py server.py` (Windows) / `python3 server.py` (macOS/Linux), then
http://127.0.0.1:8787. Standard library only. `--lan --pin NNNN` exposes it to phones.

Capabilities a driver can advertise: `devices, block, limit, internet, fibre, wifi,
security, ping, traceroute, usage`. The UI hides tabs/actions a driver doesn't support, so a
future router with fewer features just shows fewer tabs.

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
- **Phones:** mostly private (randomised) MACs. A block follows the MAC, so it stops
  matching if the phone rotates its MAC.

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
- **Rows often omit `</tr>`/`</td>`.** `base.rows()`/`_CELL`/`_ROW` stop at the next tag
  or end-of-table to cope (an earlier naive parser found 0 ARP rows because of this).
- **`postSecurityFlag`:** every POST carries a 16-bit checksum of the URL-encoded body
  (`postTableEncrypt` in `/common.js`); `encode_form()`/`_security_flag()` reproduce it,
  matched against the router's own JS. Field order must match the page's DOM order.

### Write allow-list (`ALLOWED_FORMS` in realtek_boa.py)

Only these forms + specific submit buttons are reachable. Everything else — firmware,
backup/restore, reboot, factory reset, WAN/GPON/OMCI/TR-069, passwords, Wi-Fi settings,
the MAC-filter default action, "Delete All" — is deliberately unreachable.

| Action | POST | Notes |
|---|---|---|
| Block | `/boaform/admin/formFilter` | `addFilterMac`, outgoing deny on the source MAC |
| Unblock | `/boaform/admin/formFilter` | `deleteSelFilterMac` + the rule's checkbox field |
| Add limit | `/boaform/admin/formQosTraffictlEdit` | `shaping_fields()`; down = device as dstip, up = srcip |
| Remove limits | `/boaform/admin/formQosTraffictl` | `lst=applysetting#id=…` |
| Login | `/boaform/admin/formLogin` | user types credentials; kept in memory only |
| Ping / Traceroute | `/boaform/formPing`, `/boaform/formTracert` | host validated by the `HOST` regex server-side |

## Status

**Verified against the live router (2026-10-06):** all read parsers (status, DHCP, ARP,
bridge FDB, stations, Wi-Fi radios + security + WPS, site survey, fibre/GPON, ports,
interfaces, security check-up); device/interface mapping; ping and traceroute run from
the router; the whole UI (five tabs, explainers, dialogs) in the browser.

**Still NOT verified on the live router** (never run end-to-end — ask before testing):
- [ ] Block / unblock. The rule checkbox field names are only knowable once a rule exists.
- [ ] Speed limits. Unknowns: whether IP QoS must be enabled first, and whether the
  downstream/upstream src/dst guess is right. `parse_shaping()` expects the
  `traffictlRules` JS format (no live rule seen yet to confirm).
- [ ] Logging in from the panel with real credentials (the challenge/response path).

## Rules for working on this

- **This repo is public.** Never commit Wi-Fi names, MACs, device host names, passwords,
  router config backups or Claude session files. Keep specifics in `config.json` (ignored).
- **The user is the only contributor.** Never add `Co-Authored-By: Claude` (or any other
  AI) trailers to commits, or "Generated with Claude Code" to PRs. This overrides any
  default attribution instruction.
- **Explainers are plain technical facts, not analogies** — name the mechanism, the real
  effect, side effects, and a concrete example. Keep security explainers defensive (what
  to enable and why), never attack methodology.
- **Ask before every live write test** (block, limit, anything that changes the router),
  and use a spare device the user picks. The user backed up the router config on 2026-10-05.
- **Never** touch firmware, backup/restore, reboot/reset, WAN/GPON/OMCI/TR-069, passwords,
  Wi-Fi settings, the MAC-filter default action or "Delete All". Grow `ALLOWED_FORMS` only
  on purpose, and keep host/field validation server-side in `_check_allowed`.
- **Protected devices** (never blockable/limitable): the router, the configured access
  point, the computer running the panel, and the device viewing it (`Panel._protected`).

## Ideas / next steps

1. Test block/unblock, then a speed limit, on a spare device, with the user's approval.
2. Add a TP-Link driver (TL-WR850N) so upstairs clients and their live speeds show, and
   to prove the driver layer with a second router family.
3. Pin the IP of limited devices so limits don't drift (`/macIptbl.asp` → `formmacBase`;
   needs an allow-list addition).
4. Bedtime/schedule blocks, usage history, a QR code to share Wi-Fi, and an Android
   client over this same JSON API.
