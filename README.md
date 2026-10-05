# Router panel

A neo-brutalist web panel for the home network. It shows every device on both
floors and lets you block one or limit its speed. It works by driving the
GTPL OVT OP2200H router's own web console.

## Run it

```bash
python3 server.py      # macOS / Linux
py server.py           # Windows (the "py" launcher comes with python.org Python)
```

Open http://127.0.0.1:8787. It needs Python 3.7 or newer and nothing else: it uses
the standard library only, and runs the same on macOS, Linux and Windows. The
computer must be on the home network.

The router ties its login to the computer's IP address. Either log in from the
panel's dialog, or at http://192.168.1.1 in any browser on the same computer.

To use it from phones on the Wi-Fi:

```bash
python3 server.py --lan --pin 4821
```

When you do this, Windows asks whether to let Python through the firewall. Allow
it on **Private networks** only. On Linux with `ufw` turned on, run
`sudo ufw allow 8787/tcp`.

## What it can and can't do

It can only **block**, **unblock**, **limit speed** and **remove a limit**. Every other
router setting is deliberately out of reach: firmware, reset, the fibre/WAN setup,
passwords and Wi-Fi settings. It also refuses to block the router, the upstairs
TP-Link, or the computer running the panel.

See `CLAUDE.md` for how the router's console works and what's still untested.
