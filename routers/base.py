"""Shared pieces for router drivers: errors, HTML helpers and the interface the panel relies on.

A driver turns one router family's web console into plain Python data. The panel
(server.py) only ever talks to the methods of `Driver`, so supporting another
router means writing another driver, not touching the panel or the page.
"""

import re
from html import unescape


class NotLoggedIn(Exception):
    """The router's console session for this computer has expired."""


class RouterError(Exception):
    """The router refused a request, or answered in a way the driver doesn't understand."""


# --- HTML helpers -------------------------------------------------------------------

_COMMENT = re.compile(r'<!--.*?-->', re.S)
# Embedded consoles often leave out </tr> and </td>, so a row (or cell) runs until the
# next opening tag of the same kind, its closing tag, or the end of the table.
_ROW = re.compile(r'<tr[^>]*>(.*?)(?=<tr[\s>]|</tr>|</table>|\Z)', re.S | re.I)
_CELL = re.compile(r'<t[hd][^>]*>(.*?)(?=<t[hd][\s>]|</t[hd]>|\Z)', re.S | re.I)
_TAG = re.compile(r'<[^>]+>')
_INPUT = re.compile(r'<input\b[^>]*>', re.I)
_ATTR = re.compile(r'([\w-]+)(?:\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+))?')
IP = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
_MAC = re.compile(r'^[0-9a-f]{2}([:-]?)[0-9a-f]{2}(\1[0-9a-f]{2}){4}$', re.I)
_NUMBER = re.compile(r'-?\d+(?:\.\d+)?')


def text(fragment):
    """Visible text of an HTML fragment, with whitespace collapsed."""
    return ' '.join(unescape(_TAG.sub(' ', fragment)).split())


def rows(page):
    """(row html, [cell text, ...]) for every table row on a page."""
    page = _COMMENT.sub('', page)
    return [(row, [text(c) for c in _CELL.findall(row)]) for row in _ROW.findall(page)]


def pairs(page):
    """{label: value} from two-cell table rows, the usual layout of console status pages."""
    out = {}
    for _, cells in rows(page):
        if len(cells) == 2 and cells[0]:
            out.setdefault(cells[0].rstrip(':').strip(), cells[1])
    return out


def inputs(page):
    """Attributes of every <input> tag, in page order (lower-cased names, unquoted values)."""
    found = []
    for tag in _INPUT.findall(page):
        attrs = {}
        for name, value in _ATTR.findall(tag[6:-1]):
            attrs[name.lower()] = value[1:-1] if value[:1] in '"\'' else value
        found.append(attrs)
    return found


def mac(value):
    """aa:bb:cc:dd:ee:ff for any common MAC spelling, or None."""
    value = (value or '').strip()
    if not _MAC.match(value):
        return None
    digits = re.sub(r'[^0-9a-f]', '', value.lower())
    return ':'.join(digits[i:i + 2] for i in range(0, 12, 2))


def to_int(value):
    try:
        number = float(value)
        return int(number) if number == number and abs(number) != float('inf') else None
    except (TypeError, ValueError):
        return None


def number(value):
    """First number in a string such as '-26.989698 dBm', or None."""
    match = _NUMBER.search(value or '')
    return float(match.group()) if match else None


# --- Driver interface -----------------------------------------------------------------

class Driver:
    """What the panel needs from a router.

    Reads return plain dicts and lists (see each method). A driver lists what it can do
    in `capabilities`; the panel hides features a router doesn't have. Methods for
    missing capabilities can be left unimplemented.

    Capabilities: devices, block, limit, internet, fibre, wifi, security, ping, traceroute.
    """

    family = 'Unknown router'
    capabilities = frozenset()

    def __init__(self, host):
        self.host = host

    def login(self, username, password):
        """Log in to the console. Raises RouterError if the router rejects the credentials."""
        raise NotImplementedError

    def logout(self):
        """End the console session and forget any saved credentials."""
        raise NotImplementedError

    # Reads
    def status(self):
        """{model, firmware, uptime, cpu, memory, dns: [..], lan: {ip, mask, mac},
        wan: {iface, vlan, type, protocol, ip, gateway, status, up}}"""
        raise NotImplementedError

    def clients(self):
        """{dhcp: {mac: {ip, lease}}, arp: {mac: ip}, stations: {radio id: {mac: {...}}},
        ports: {mac: interface id}, radios: {radio id: {bssid, band, ssid, channel}}}"""
        raise NotImplementedError

    def blocks(self):
        """[{src: mac, action, ...driver fields needed to delete it}]"""
        raise NotImplementedError

    def limits(self):
        """[{id, srcip, dstip, rate (kb/s), direction: 'up' | 'down'}]"""
        raise NotImplementedError

    def internet(self):
        """{ipv6: {...}, fibre: {...} | None, ports: [...], interfaces: [...]}"""
        raise NotImplementedError

    def radios(self):
        """[{id, band: '2.4' | '5', ssid, channel, width, standard, power, security, ...}]"""
        raise NotImplementedError

    def neighbours(self):
        """[{radio, ssid, bssid, channel, width, security, signal (%)}] from the last scan."""
        raise NotImplementedError

    def security(self):
        """Raw firewall / remote-access settings the panel turns into a check-up."""
        raise NotImplementedError

    # Writes
    def block(self, mac):
        raise NotImplementedError

    def unblock(self, rules):
        raise NotImplementedError

    def add_limit(self, ip, rate, direction):
        raise NotImplementedError

    def remove_limits(self, ids):
        raise NotImplementedError

    # Diagnostics, run by the router itself
    def start_diag(self, kind, host):
        """Start a 'ping' or 'traceroute' from the router."""
        raise NotImplementedError

    def diag_output(self, kind):
        """Text lines the running diagnostic has printed so far."""
        raise NotImplementedError
