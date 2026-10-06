# fun-router

Router consoles are powerful but boring and confusing. **fun-router** is a
neo-brutalist web app that makes your home router's own console fun and
understandable: it shows what's going on in clear terms, and every setting has a
"How it works" toggle (hidden by default) that explains, in plain technical
language, what it is and what changes when you change it.

It works by driving the router's own web console, so it needs no firmware changes.
Today it supports Realtek "Boa" GPON routers (e.g. the GTPL OVT OP2200H); the code
is split so other routers can be added as drivers.

## Run it

```bash
py server.py           # Windows (the "py" launcher ships with python.org Python)
```

```bash
python3 server.py      # macOS / Linux
```

Open http://127.0.0.1:8787. Python 3.7+ and nothing else — standard library only,
same on Windows, macOS and Linux. The computer must be on the home network.

Copy `config.example.json` to `config.json` and set your router's IP, the devices
to protect, and any port labels. The router ties its login to the computer's IP, so
log in once from the panel's dialog (or in any browser at the router's address on
the same computer).

To use it from phones on the Wi-Fi:

```bash
python3 server.py --lan --pin 4821
```

Windows will ask to let Python through the firewall — allow it on **Private
networks** only. On Linux with `ufw`, run `sudo ufw allow 8787/tcp`.

## What it shows

- **Devices** — everyone on the network, where each is connected, live Wi-Fi speed
  and signal. Block, unblock, speed-limit or rename a device.
- **Usage** — live speed of the whole connection, data used today / this week / this month
  (download, upload or both), usage over time, top devices, share by device, an hour-by-weekday
  heatmap, fun facts and fibre signal history. The router only counts since it last booted, so
  fun-router records the history itself into `usage.db` (local, git-ignored) while it runs.
- **Internet** — the connection (PPPoE, CGNAT detection), IPv4/IPv6, DNS, the fibre
  (GPON) optical levels with a health gauge, data used, and LAN port speeds.
- **Wi-Fi** — each radio's channel, width, standard, power, security and WPS, plus a
  channel map of your networks against the neighbours around you.
- **Security** — a read-only check-up that rates WPS, encryption, remote access,
  UPnP, DMZ, port forwards and more against safe defaults.
- **Tools** — ping and traceroute, run from the router itself.

## What it can change

Only **block**, **unblock**, **limit speed** and **remove a limit** on a device, plus
running a ping/traceroute. Everything else is read-only: firmware, reset, the
fibre/WAN setup, passwords and Wi-Fi settings are deliberately out of reach, and it
refuses to block the router, the access point, or the computer running the panel.

See `CLAUDE.md` for how the console works, the architecture, and what's still untested.
