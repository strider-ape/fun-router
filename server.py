#!/usr/bin/env python3
"""Home network panel for the GTPL OVT OP2200H router.

Serves index.html and a small JSON API. The API reads, and submits, the same
pages and forms the router's own web console at 192.168.1.1 uses. The router
ties its login to this computer's IP address, so the panel works while this
computer is logged in to the console (or after you log in from the panel).

Python 3 standard library only:

    python3 server.py                    # http://127.0.0.1:8787
    python3 server.py --lan --pin 4821   # also reachable from phones on the Wi-Fi
"""

import argparse
import base64
import hmac
import http.client
import http.server
import json
import os
import random
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

ROUTER_IP = '192.168.1.1'
ROUTER = 'http://' + ROUTER_IP
ACCESS_POINT_IP = '192.168.1.3'  # TP-Link TL-WR850N in access-point mode, first floor
WAN_IFACE = '65536'              # ppp0_nas0_0, the only WAN in net_qos_traffictl_edit.asp
MIN_LIMIT_KBPS = 256
MAX_LIMIT_KBPS = 1000000
HERE = os.path.dirname(os.path.abspath(__file__))
NICKNAMES_FILE = os.path.join(HERE, 'nicknames.json')

# The only router forms this panel can submit, and the only submit buttons it
# may press on them. Firmware, backup/restore, reboot, WAN/GPON/TR-069,
# passwords, Wi-Fi settings, the MAC filter's default action and its
# "Delete All" button are deliberately unreachable.
ALLOWED_FORMS = {
    '/boaform/admin/formLogin': {'save'},
    '/boaform/admin/formFilter': {'addFilterMac', 'deleteSelFilterMac'},
    '/boaform/admin/formQosTraffictlEdit': set(),
    '/boaform/admin/formQosTraffictl': set(),
}
BUTTON_FIELDS = {'save', 'addFilterMac', 'deleteSelFilterMac', 'setMacDft', 'deleteAllFilterMac'}
FORBIDDEN_FIELDS = {'setMacDft', 'deleteAllFilterMac', 'outAct', 'inAct'}


class NotLoggedIn(Exception):
    pass


class RouterError(Exception):
    pass


# --- Form encoding, as done by postTableEncrypt() in the router's common.js ---

def _js_encode(value):
    """encodeURIComponent() plus the console's extra escapes for ! ' ( ) ~ and space."""
    return urllib.parse.quote_plus(str(value), safe='*').replace('~', '%7E')


def _int32(n):
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n & 0x80000000 else n


def _security_flag(body):
    """The 16-bit checksum the router expects in the postSecurityFlag field."""
    total, i, n = 0, 0, len(body)
    while i < n:
        if i + 4 > n:
            for k, shift in ((0, 24), (1, 16), (2, 8)):
                if i + k < n:
                    total += ord(body[i + k]) << shift
            break
        total += (ord(body[i]) << 24) + (ord(body[i + 1]) << 16) + (ord(body[i + 2]) << 8) + ord(body[i + 3])
        i += 4
    c = _int32(total)
    c = (c & 0xFFFF) + (c >> 16)
    c &= 0xFFFF
    return (~c) & 0xFFFF


def encode_form(fields):
    body = ''.join('%s=%s&' % (name.replace('[', '%5B').replace(']', '%5D'), _js_encode(value))
                   for name, value in fields)
    return body + 'postSecurityFlag=%d' % _security_flag(body)


def _check_allowed(action, fields):
    if action not in ALLOWED_FORMS:
        raise RouterError('Blocked: %s is not on the allow-list' % action)
    names = {name for name, _ in fields}
    if names & FORBIDDEN_FIELDS:
        raise RouterError('Blocked: forbidden field sent to %s' % action)
    buttons = names & BUTTON_FIELDS
    if buttons - ALLOWED_FORMS[action] or (ALLOWED_FORMS[action] and len(buttons) != 1):
        raise RouterError('Blocked: unexpected button on %s' % action)
    if action == '/boaform/admin/formQosTraffictl':
        if not dict(fields).get('lst', '').startswith('applysetting#id='):
            raise RouterError('Blocked: unexpected Traffic Shaping request')


# --- Router client ---

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Router:
    def __init__(self):
        self.lock = threading.RLock()  # the router's web server handles one request at a time
        self.opener = urllib.request.build_opener(_NoRedirect)
        self.creds = None  # (username, password), in memory only, set from the panel's login form

    def _request(self, path, data=None, referer='/'):
        req = urllib.request.Request(ROUTER + path, data=data)
        req.add_header('Referer', ROUTER + referer)
        if data is not None:
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            req.add_header('Origin', ROUTER)
        try:
            with self.opener.open(req, timeout=8) as resp:
                return resp.status, '', resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307):
                return e.code, e.headers.get('Location', ''), ''
            raise RouterError('Router answered HTTP %d for %s' % (e.code, path))
        except http.client.BadStatusLine as e:
            # When logged out, the router sends a bare "You have not logined" page with no headers.
            return 0, '', str(e)
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, ConnectionError) as e:
            raise RouterError('Cannot reach the router at %s (%s)' % (ROUTER_IP, getattr(e, 'reason', e)))

    @staticmethod
    def _is_login(location, body):
        lower = body.lower()
        return 'login.asp' in location or 'formLogin' in body or 'not logined' in lower or '<title>login</title>' in lower

    def get(self, path, retry=True):
        with self.lock:
            _, location, body = self._request(path)
            if self._is_login(location, body):
                if retry and self.creds:
                    self.login(*self.creds)
                    return self.get(path, retry=False)
                raise NotLoggedIn()
            return body

    def post_form(self, action, fields, referer, check_session=True):
        _check_allowed(action, fields)
        with self.lock:
            _, location, text = self._request(action, encode_form(fields).encode('ascii'), referer)
        if check_session and self._is_login(location, text):
            raise NotLoggedIn()
        message = re.search(r'<h4>(.*?)</h4>', text, re.S | re.I)
        if message and re.search(r'error|fail|invalid', _text(message.group(1)), re.I):
            raise RouterError('Router said: ' + _text(message.group(1)))
        return text

    def login(self, username, password):
        with self.lock:
            self.post_form('/boaform/admin/formLogin', [
                ('challenge', ''),
                ('username', username),
                ('save', 'Login'),
                ('encodePassword', base64.b64encode(password.encode('utf-8')).decode('ascii')),
                ('submit-url', '/admin/login.asp'),
            ], referer='/admin/login.asp', check_session=False)
            self.creds = None
            try:
                self.get('/status.asp', retry=False)
            except NotLoggedIn:
                raise RouterError('The router did not accept that username and password')
            self.creds = (username, password)

    def stations(self, wlan_idx):
        """Wi-Fi clients on one of the router's own radios (0 = 5 GHz, 1 = 2.4 GHz)."""
        with self.lock:
            self.get('/boaform/formWlanRedirect?redirect-url=/wlstatbl.asp&wlan_idx=%d' % wlan_idx)
            return parse_stations(self.get('/wlstatbl.asp'))


# --- Console page parsers ---

_COMMENT = re.compile(r'<!--.*?-->', re.S)
_ROW = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
_CELL = re.compile(r'<t[hd][^>]*>(.*?)</t[hd]>', re.S | re.I)
_TAG = re.compile(r'<[^>]+>')
_IP = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
_MAC = re.compile(r'^[0-9a-f]{2}([:-]?)[0-9a-f]{2}(\1[0-9a-f]{2}){4}$', re.I)


def _text(fragment):
    return ' '.join(unescape(_TAG.sub(' ', fragment)).split())


def _rows(page):
    """(row html, [cell text, ...]) for every table row on a console page."""
    page = _COMMENT.sub('', page)
    return [(row, [_text(c) for c in _CELL.findall(row)]) for row in _ROW.findall(page)]


def _mac(value):
    value = (value or '').strip()
    if not _MAC.match(value):
        return None
    digits = re.sub(r'[^0-9a-f]', '', value.lower())
    return ':'.join(digits[i:i + 2] for i in range(0, 12, 2))


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_status(page):
    info = {}
    for _, cells in _rows(page):
        if len(cells) == 2:
            info.setdefault(cells[0], cells[1])
        elif len(cells) >= 7 and cells[0].startswith('ppp'):
            info['wan'] = {'protocol': cells[3], 'ip': cells[4], 'status': cells[6]}
    return {
        'model': info.get('Device Name'),
        'firmware': info.get('Firmware Version'),
        'uptime': info.get('Uptime'),
        'cpu': info.get('CPU Usage'),
        'memory': info.get('Memory Usage'),
        'wan': info.get('wan'),
    }


def parse_dhcp(page):
    leases = {}
    for _, cells in _rows(page):
        if len(cells) >= 3 and _IP.match(cells[0]) and _mac(cells[1]):
            leases[_mac(cells[1])] = {'ip': cells[0], 'lease': _int(cells[2])}
    return leases


def parse_arp(page):
    return {_mac(c[1]): c[0] for _, c in _rows(page) if len(c) >= 2 and _IP.match(c[0]) and _mac(c[1])}


def parse_stations(page):
    stations = {}
    for _, c in _rows(page):
        if len(c) >= 14 and _mac(c[0]):
            stations[_mac(c[0])] = {
                'linkMbps': _int(c[1]),
                'txBytes': _int(c[5]),
                'rxBytes': _int(c[6]),
                'rssi': _int(c[7]),
                'uptime': _int(c[13]),
            }
    return stations


def parse_mac_rules(page):
    start = page.find('name="formFilterDel"')
    section = page[start:page.find('</form>', start)] if start >= 0 else ''
    rules = []
    for row, cells in _rows(section):
        box = re.search(r'<input[^>]*type=["\']?checkbox[^>]*>', row, re.I)
        if not box or len(cells) < 5:
            continue
        name = re.search(r'name=["\']?([\w\[\]]+)', box.group(0))
        value = re.search(r'value=["\']?([^"\'\s>]+)', box.group(0))
        rules.append({
            'field': name.group(1) if name else None,
            'value': value.group(1) if value else 'on',
            'direction': cells[1],
            'src': _mac(cells[2]),
            'action': cells[4],
        })
    return rules


_SHAPING_RULE = re.compile(r'traffictlRules(?:\.push\(|\[\d+\]\s*=)(.*?)\)\s*;', re.S)
_SHAPING_PAIR = re.compile(r'new it\(\s*"(\w+)"\s*,\s*(?:"([^"]*)"|([^)\s]*))\s*\)|(\w+)\s*:\s*(?:"([^"]*)"|([^,}\s]*))')


def parse_shaping(page):
    rules = []
    for match in _SHAPING_RULE.finditer(page):
        pairs = {}
        for g in _SHAPING_PAIR.findall(match.group(1)):
            key, value = (g[0], g[1] or g[2]) if g[0] else (g[3], g[4] or g[5])
            pairs[key] = value.strip()
        if 'rate' in pairs:
            rules.append({
                'id': pairs.get('id'),
                'srcip': pairs.get('srcip'),
                'dstip': pairs.get('dstip'),
                'rate': _int(pairs['rate']),
                'direction': 'down' if pairs.get('direction') == '1' else 'up',
            })
    return rules


def shaping_fields(ip, rate, direction):
    """Fields of net_qos_traffictl_edit.asp, in page order, for one per-device IPv4 limit.

    direction: 1 = Downstream (match the device as destination), 0 = Upstream (as source).
    """
    down = direction == 1
    src, src_mask = ('', '') if down else (ip, '255.255.255.255')
    dst, dst_mask = (ip, '255.255.255.255') if down else ('', '')
    lst = ('dummy=dummy&inf=%s&proto=0&IPversion=1&srcip=%s&srcnetmask=%s&dstip=%s&dstnetmask=%s'
           '&sport=&dport=&rate=%d&direction=%d' % (WAN_IFACE, src, src_mask, dst, dst_mask, rate, direction))
    return [
        ('IpProtocolType', '1'), ('direction', str(direction)), ('vlanID', ''), ('protolist', '0'),
        ('srcip', src), ('srcnetmask', src_mask), ('dstip', dst), ('dstnetmask', dst_mask),
        ('sip6', ''), ('sip6PrefixLen', ''), ('dip6', ''), ('dip6PrefixLen', ''),
        ('sport', ''), ('dport', ''), ('rate', str(rate)),
        ('lst', base64.b64encode(lst.encode('ascii')).decode('ascii')),
        ('submit-url', '/net_qos_traffictl.asp'),
    ]


# --- Device names from the router's DNS (it registers DHCP host names) ---

def _skip_name(data, offset):
    while True:
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length


def _read_name(data, offset):
    labels = []
    for _ in range(64):  # guards against compression-pointer loops
        length = data[offset]
        if length == 0:
            break
        if length & 0xC0 == 0xC0:
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        labels.append(data[offset + 1:offset + 1 + length].decode('utf-8', 'replace'))
        offset += 1 + length
    return '.'.join(labels)


def lookup_hostname(ip):
    qname = '.'.join(reversed(ip.split('.'))) + '.in-addr.arpa'
    tid = random.randint(0, 0xFFFF)
    query = struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0)
    query += b''.join(bytes([len(p)]) + p.encode('ascii') for p in qname.split('.')) + b'\0'
    query += struct.pack('>HH', 12, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.6)
            s.sendto(query, (ROUTER_IP, 53))
            data = s.recv(512)
        if struct.unpack('>H', data[:2])[0] != tid or struct.unpack('>H', data[6:8])[0] == 0:
            return None
        offset = _skip_name(data, _skip_name(data, 12) + 4)
        rtype = struct.unpack('>H', data[offset:offset + 2])[0]
        return _read_name(data, offset + 10).split('.')[0] or None if rtype == 12 else None
    except (OSError, struct.error, IndexError):
        return None


def own_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ROUTER_IP, 53))
            return s.getsockname()[0]
    except OSError:
        return None


# --- Panel state and actions ---

class Panel:
    def __init__(self, router):
        self.router = router
        self.lock = threading.RLock()
        self.fast, self.fast_at = None, 0  # device lists, refreshed every few seconds
        self.slow, self.slow_at = None, 0  # router status and rules, every 30 s or after a change
        self.hostnames = {}                # ip -> (name, looked up at)
        self.samples = {}                  # mac -> (time, txBytes, rxBytes) for live speeds
        self.own_ip = own_ip()
        try:
            with open(NICKNAMES_FILE) as f:
                self.nicknames = json.load(f)
        except (OSError, ValueError):
            self.nicknames = {}

    def _refresh(self, force=False):
        with self.lock:
            now = time.time()
            r = self.router
            if force or not self.slow or now - self.slow_at > 30:
                self.slow = {
                    'router': parse_status(r.get('/status.asp')),
                    'blocks': parse_mac_rules(r.get('/fw-macfilter.asp')),
                    'limits': parse_shaping(r.get('/net_qos_traffictl.asp')),
                }
                self.slow_at = now
            if force or not self.fast or now - self.fast_at > 4:
                fast = {
                    'dhcp': parse_dhcp(r.get('/dhcptbl.asp')),
                    'arp': parse_arp(r.get('/arptable.asp')),
                    'wifi5': r.stations(0),
                    'wifi24': r.stations(1),
                }
                self._measure_speeds(fast, now)
                ips = {v['ip'] for v in fast['dhcp'].values()} | set(fast['arp'].values())
                for ip in ips:
                    if ip not in self.hostnames or now - self.hostnames[ip][1] > 600:
                        self.hostnames[ip] = (lookup_hostname(ip), now)
                self.fast, self.fast_at = fast, now
            return self.fast, self.slow

    def _measure_speeds(self, fast, now):
        for band in ('wifi5', 'wifi24'):
            for mac, st in fast[band].items():
                prev = self.samples.get(mac)
                if st['txBytes'] is None or st['rxBytes'] is None:
                    continue
                self.samples[mac] = (now, st['txBytes'], st['rxBytes'])
                if prev and now - prev[0] > 1 and st['txBytes'] >= prev[1] and st['rxBytes'] >= prev[2]:
                    seconds = now - prev[0]
                    st['downKbps'] = round((st['txBytes'] - prev[1]) * 8 / seconds / 1000)
                    st['upKbps'] = round((st['rxBytes'] - prev[2]) * 8 / seconds / 1000)

    def _protected(self, ip, client_ip):
        if ip == ROUTER_IP:
            return 'The main router'
        if ip == ACCESS_POINT_IP:
            return 'The first-floor access point. Blocking it would cut off the whole floor.'
        if ip and ip == self.own_ip:
            return 'This computer runs the panel'
        if ip and ip == client_ip:
            return "The device you're using right now"
        return None

    def _devices(self, fast, slow, client_ip):
        macs = list(fast['dhcp'])
        for source in (fast['arp'], fast['wifi5'], fast['wifi24'], [r['src'] for r in slow['blocks']]):
            macs += [m for m in source if m and m not in macs]
        devices = []
        for mac in macs:
            lease = fast['dhcp'].get(mac) or {}
            ip = lease.get('ip') or fast['arp'].get(mac)
            wifi = fast['wifi5'].get(mac) or fast['wifi24'].get(mac)
            band = '5' if mac in fast['wifi5'] else '2.4' if mac in fast['wifi24'] else None
            if ip == ACCESS_POINT_IP:
                where = 'ap'
            elif band:
                where = 'ground-5' if band == '5' else 'ground-24'
            else:
                where = 'upstairs'
            limits = [r for r in slow['limits'] if ip and ip in (r['srcip'], r['dstip'])]
            devices.append({
                'mac': mac,
                'ip': ip,
                'hostname': self.hostnames.get(ip, (None,))[0] if ip else None,
                'nickname': self.nicknames.get(mac),
                'where': where,
                'online': bool(wifi) or mac in fast['arp'],
                'privateMac': bool(int(mac[:2], 16) & 2),
                'lease': lease.get('lease'),
                'wifi': wifi,
                'blocked': any(r['src'] == mac and r['action'].lower() == 'deny' for r in slow['blocks']),
                'limit': {
                    'down': next((r['rate'] for r in limits if r['direction'] == 'down'), None),
                    'up': next((r['rate'] for r in limits if r['direction'] == 'up'), None),
                },
                'protected': self._protected(ip, client_ip),
            })
        return devices

    def state(self, client_ip):
        fast, slow = self._refresh()
        return {
            'router': slow['router'],
            'devices': self._devices(fast, slow, client_ip),
            'updated': int(self.fast_at),
            'minLimitKbps': MIN_LIMIT_KBPS,
        }

    def _find(self, body, client_ip):
        mac = _mac(body.get('mac'))
        if not mac:
            raise ValueError('Missing or invalid MAC address')
        fast, slow = self._refresh(force=True)
        device = next((d for d in self._devices(fast, slow, client_ip) if d['mac'] == mac), None)
        return mac, device, slow

    @staticmethod
    def _label(device, mac):
        return (device and (device['nickname'] or device['hostname'] or device['ip'])) or mac

    def block(self, body, client_ip):
        with self.lock:
            mac, device, _ = self._find(body, client_ip)
            if not device:
                raise ValueError('That device is not on the network right now')
            if device['protected']:
                raise ValueError('Protected: ' + device['protected'])
            if not device['blocked']:
                self.router.post_form('/boaform/admin/formFilter', [
                    ('dir', '0'), ('srcmac', mac.replace(':', '')), ('dstmac', ''),
                    ('filterMode', 'Deny'), ('addFilterMac', 'Add'),
                    ('submit-url', '/admin/fw-macfilter.asp'),
                ], referer='/admin/fw-macfilter.asp')
            _, slow = self._refresh(force=True)
            if not any(r['src'] == mac and r['action'].lower() == 'deny' for r in slow['blocks']):
                raise RouterError('The router did not save the block rule')
            return {'ok': True, 'message': '%s is blocked' % self._label(device, mac)}

    def unblock(self, body, client_ip):
        with self.lock:
            mac, device, slow = self._find(body, client_ip)
            rules = [r for r in slow['blocks'] if r['src'] == mac]
            if rules:
                if not all(r['field'] for r in rules):
                    raise RouterError("Couldn't find this rule's checkbox on the MAC Filtering page")
                self.router.post_form(
                    '/boaform/admin/formFilter',
                    [(r['field'], r['value']) for r in rules]
                    + [('deleteSelFilterMac', 'Delete Selected'), ('submit-url', '/admin/fw-macfilter.asp')],
                    referer='/admin/fw-macfilter.asp')
                _, slow = self._refresh(force=True)
                if any(r['src'] == mac for r in slow['blocks']):
                    raise RouterError('The router did not remove the block rule')
            return {'ok': True, 'message': '%s is unblocked' % self._label(device, mac)}

    def _remove_limits(self, ip, slow):
        ids = [r['id'] for r in slow['limits'] if ip in (r['srcip'], r['dstip'])]
        if not ids:
            return
        if not all(ids):
            raise RouterError("Couldn't read the Traffic Shaping rule IDs")
        self.router.post_form('/boaform/admin/formQosTraffictl', [
            ('lst', 'applysetting#id=' + '|'.join(ids)),
            ('submit-url', '/net_qos_traffictl.asp'),
        ], referer='/net_qos_traffictl.asp')

    @staticmethod
    def _rate(value):
        if value in (None, '', 0, '0'):
            return None
        rate = _int(value)
        if rate is None or not MIN_LIMIT_KBPS <= rate <= MAX_LIMIT_KBPS:
            raise ValueError('Limits must be between %d kb/s and %d kb/s' % (MIN_LIMIT_KBPS, MAX_LIMIT_KBPS))
        return rate

    def limit(self, body, client_ip):
        down, up = self._rate(body.get('down')), self._rate(body.get('up'))
        if not down and not up:
            raise ValueError('Set a download or upload limit')
        with self.lock:
            mac, device, slow = self._find(body, client_ip)
            if not device or not device['ip']:
                raise ValueError('That device has no IP address right now')
            if device['protected']:
                raise ValueError('Protected: ' + device['protected'])
            ip = device['ip']
            self._remove_limits(ip, slow)
            for direction, rate in ((1, down), (0, up)):
                if rate:
                    self.router.post_form('/boaform/admin/formQosTraffictlEdit', shaping_fields(ip, rate, direction),
                                          referer='/net_qos_traffictl_edit.asp')
            _, slow = self._refresh(force=True)
            saved = {(r['direction'], r['rate']) for r in slow['limits'] if ip in (r['srcip'], r['dstip'])}
            wanted = {('down', down), ('up', up)} - {('down', None), ('up', None)}
            if not wanted <= saved:
                raise RouterError('The router did not save the speed limit')
            return {'ok': True, 'message': 'Speed limit set for %s' % self._label(device, mac)}

    def unlimit(self, body, client_ip):
        with self.lock:
            mac, device, slow = self._find(body, client_ip)
            ip = device and device['ip']
            if ip:
                self._remove_limits(ip, slow)
                _, slow = self._refresh(force=True)
                if any(ip in (r['srcip'], r['dstip']) for r in slow['limits']):
                    raise RouterError('The router did not remove the speed limit')
            return {'ok': True, 'message': 'Speed limit removed for %s' % self._label(device, mac)}

    def rename(self, body, client_ip):
        mac = _mac(body.get('mac'))
        if not mac:
            raise ValueError('Missing or invalid MAC address')
        name = ' '.join(str(body.get('name') or '').split())[:40]
        with self.lock:
            if name:
                self.nicknames[mac] = name
            else:
                self.nicknames.pop(mac, None)
            with open(NICKNAMES_FILE, 'w') as f:
                json.dump(self.nicknames, f, indent=2)
        return {'ok': True}

    def login(self, body, client_ip):
        username, password = str(body.get('username') or ''), str(body.get('password') or '')
        if not username or not password:
            raise ValueError('Enter the router username and password')
        self.router.login(username, password)
        with self.lock:
            self.fast = self.slow = None
        return {'ok': True}


# --- HTTP server ---

class Handler(http.server.BaseHTTPRequestHandler):
    panel = None
    pin = None
    hosts = {'127.0.0.1', 'localhost'}

    def log_message(self, fmt, *args):
        if self.command == 'POST' or (args and str(args[1])[:1] in '45'):
            super().log_message(fmt, *args)

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode('utf-8'), 'application/json')

    def _allowed(self, api):
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0]
        if host not in self.hosts:  # stops DNS-rebinding pages from reaching the API
            self._json(403, {'error': 'Unknown host'})
            return False
        if not api:
            return True
        if self.headers.get('X-Panel') != '1':  # stops other websites posting to the API
            self._json(403, {'error': 'Missing X-Panel header'})
            return False
        if self.pin and not self.client_address[0].startswith('127.') and \
                not hmac.compare_digest(self.headers.get('X-Panel-Pin') or '', self.pin):
            self._json(403, {'error': 'pin'})
            return False
        return True

    def _call(self, fn, *args):
        try:
            self._json(200, fn(*args))
        except NotLoggedIn:
            self._json(401, {'error': 'login'})
        except ValueError as e:
            self._json(400, {'error': str(e)})
        except RouterError as e:
            self._json(502, {'error': str(e)})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html') and self._allowed(api=False):
            with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
        elif path == '/api/state' and self._allowed(api=True):
            self._call(self.panel.state, self.client_address[0])
        elif path not in ('/', '/index.html', '/api/state'):
            self._json(404, {'error': 'Not found'})

    def do_POST(self):
        actions = {
            '/api/block': self.panel.block,
            '/api/unblock': self.panel.unblock,
            '/api/limit': self.panel.limit,
            '/api/unlimit': self.panel.unlimit,
            '/api/name': self.panel.rename,
            '/api/login': self.panel.login,
        }
        fn = actions.get(urllib.parse.urlparse(self.path).path)
        if not fn:
            return self._json(404, {'error': 'Not found'})
        if not self._allowed(api=True):
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except ValueError:
            return self._json(400, {'error': 'Bad JSON'})
        self._call(fn, body if isinstance(body, dict) else {}, self.client_address[0])


def main():
    parser = argparse.ArgumentParser(description='Home network panel for the OVT OP2200H router')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--lan', action='store_true', help='also listen on the Wi-Fi so phones can use it')
    parser.add_argument('--pin', help='PIN that other devices must enter (required with --lan)')
    args = parser.parse_args()
    if args.lan and not args.pin:
        parser.error('--lan needs --pin, otherwise anyone on your Wi-Fi could block devices')

    Handler.panel = Panel(Router())
    Handler.pin = args.pin
    bind = '0.0.0.0' if args.lan else '127.0.0.1'
    lan_ip = Handler.panel.own_ip
    if args.lan and lan_ip:
        Handler.hosts = Handler.hosts | {lan_ip}
    server = http.server.ThreadingHTTPServer((bind, args.port), Handler)
    print('Router panel: http://127.0.0.1:%d' % args.port)
    if args.lan and lan_ip:
        print('On your Wi-Fi:  http://%s:%d  (PIN required)' % (lan_ip, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
