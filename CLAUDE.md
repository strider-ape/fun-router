# Router panel: context for Claude

A local web panel to see who is on the home network and block or speed-limit
devices. `server.py` (Python 3 standard library only) drives the main router's
own web console, and `index.html` is a single neo-brutalist page that talks to
it through a small JSON API. The page cannot call the router directly: the
router sends no CORS headers and sets `X-Frame-Options: SAMEORIGIN`.

## The network

- **Main router:** OVT OP2200H GPON fibre router, installed by the ISP GTPL.
  - Firmware V4.0.1--e240304, at `192.168.1.1`, Realtek SDK with the Boa web server.
  - It handles DHCP and NAT, with PPPoE WAN `ppp0_nas0_0`.
  - Radios: wlan0 = 5 GHz, wlan1 = 2.4 GHz.
- **First floor:** TP-Link TL-WR850N (2.4 GHz only) in access-point mode.
  - At `192.168.1.3`. Same SSID and password as the main 2.4 GHz radio.
  - It bridges at layer 2, so its clients get DHCP from the OVT and route through it. The OVT is therefore the single place to block or limit any device.
  - Not integrated yet: it needs its own login, and newer TP-Link firmware encrypts the login.
- **Device names:** the router's DNS (`192.168.1.1:53`) answers reverse (PTR) lookups with DHCP host names, for example `192.168.1.6 → <dhcp-host-name>.bbrouter`. `lookup_hostname()` uses this.
- **Phones:** most use private (randomised) MACs. A block follows the MAC, so it stops matching if the phone changes its MAC.

## Router console facts (found on 2026-10-05)

- **Login is tied to the client IP, not cookies.** If this computer is logged in from any browser, plain requests work.
- **Idle timeout:** after a few idle minutes the router logs the IP out. Every page then returns a bare `<HTML><HEAD><TITLE>Login</TITLE>… You have not logined` with **no HTTP status line**. Python raises `BadStatusLine`; `Router._request` handles this.
- **The router answers one request at a time.** `Router.lock` serialises all calls.
- **All pages are listed in `/adminMenu.js`.** Full admin access: there are Firewall, IP QoS, ACL and TR-069 menus.
- **`postSecurityFlag`:** every form POST carries a 16-bit checksum of the URL-encoded body (`postTableEncrypt` in `/common.js`).
  - `encode_form()` / `_security_flag()` reproduce it. They matched the router's own JS on three test cases, including `!'()~*+/=` and non-ASCII characters.
  - Field order must match the page's DOM order. Unchecked radios, unnamed fields and unclicked submit buttons are left out.

### Read pages

| Page | What it shows |
|---|---|
| `/status.asp` | Model, firmware, uptime, CPU, memory, WAN row |
| `/dhcptbl.asp` | Active DHCP clients: IP, MAC, lease seconds |
| `/arptable.asp` | ARP table: IP, MAC (dash format) |
| `/boaform/formWlanRedirect?redirect-url=/wlstatbl.asp&wlan_idx=N` then `/wlstatbl.asp` | Wi-Fi clients on radio N: MAC, rates, TX/RX bytes, RSSI, uptime. The index is stored per session, so both calls are made under the lock. |
| `/fw-macfilter.asp` | MAC filter rules (the `formFilterDel` table) and the default actions. Outgoing default is **Allow**. |
| `/net_qos_traffictl.asp` | Traffic Shaping rules, filled into `traffictlRules` by inline JS |
| `/net_qos_imq_policy.asp` | IP QoS on/off. It was **off** (`qosEnable=0`) on 2026-10-05. |

### Write forms (the only ones in `ALLOWED_FORMS`)

| Action | POST | Fields, in order |
|---|---|---|
| Block | `/boaform/admin/formFilter` | `dir=0, srcmac=<12 hex, no separators>, dstmac=, filterMode=Deny, addFilterMac=Add, submit-url=/admin/fw-macfilter.asp` |
| Unblock | `/boaform/admin/formFilter` | `<rule checkbox name>=<value>…, deleteSelFilterMac=Delete Selected, submit-url=/admin/fw-macfilter.asp` |
| Add limit | `/boaform/admin/formQosTraffictlEdit` | See `shaping_fields()`. `lst` = base64 of `dummy=dummy&inf=65536&proto=0&IPversion=1&srcip=…&rate=<kb/s>&direction=<0 up / 1 down>`. |
| Remove limits | `/boaform/admin/formQosTraffictl` | `lst=applysetting#id=<id>|<id>, submit-url=/net_qos_traffictl.asp` |
| Login | `/boaform/admin/formLogin` | `challenge=, username, save=Login, encodePassword=base64(password), submit-url=/admin/login.asp` |

## Status

**Working and verified**
- The checksum
- Detecting a logged-out session
- Serving the UI
- The login dialog appearing when the router has logged out

**Written but not yet verified against the live router.** The router logged out before the first end-to-end run.
- [ ] The read-side parsers. They were written against the live HTML seen in the browser.
- [ ] Logging in with real credentials. The user types these themselves; never enter them for the user.
- [ ] Block and unblock. The rule checkbox field names are unknown until a rule exists.
- [ ] Speed limits. Unknowns: whether downstream should match the device as `dstip` (current guess, with upstream as `srcip`), whether IP QoS must be enabled first, and the exact `traffictlRules` JS format that `parse_shaping()` expects.

## Rules for working on this

- **This repo is public.** Never commit Wi-Fi names, MAC addresses, device host names, passwords, router config backups or Claude session files. Use placeholders in docs.
- **Ask before every live write test** (block, limit, or anything else that changes the router), and use a spare device the user picks. The user backed up the router config on 2026-10-05.
- **Never** touch firmware, backup/restore, reboot or reset, WAN/GPON/OMCI/TR-069, passwords, Wi-Fi settings, the MAC filter default action or "Delete All". Grow `ALLOWED_FORMS` only on purpose.
- **Protected devices** that the app refuses to block or limit: the router, the TP-Link access point, the computer running the panel, and the device viewing it.
- **Factory reset** of the OVT would erase GTPL's PPPoE and GPON setup. Nothing in the app can trigger it.

## Ideas / next steps

1. Log in and check the read side end to end. Take screenshots at phone and desktop widths, in light and dark.
2. Test block/unblock, then a speed limit, on a spare device, with the user's approval.
3. Read the TP-Link's Wi-Fi client list to tell upstairs devices from wired ones, and show live speeds for them.
4. Keep limits from drifting by pinning the IP of limited devices. This uses MAC-based assignment, `/macIptbl.asp` → `formmacBase`, and needs an allow-list addition.
5. Bedtime block schedules, usage history, and an Android app (a WebView or native client for this API).
