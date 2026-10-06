#!/usr/bin/env python3
"""fun-router: a fun, explained web console for home routers.

Serves the page in web/ and a small JSON API. A driver in routers/ reads, and submits,
the same pages and forms the router's own web console uses; this file doesn't know
which router it is talking to.

Python 3 standard library only:

    py server.py                         # Windows
    python3 server.py                    # macOS / Linux
    python3 server.py --lan --pin 4821   # also reachable from phones on the Wi-Fi
"""

import argparse
import hmac
import http.server
import ipaddress
import json
import mimetypes
import os
import random
import re
import socket
import struct
import threading
import time
import urllib.parse

from routers import NotLoggedIn, RouterError, make_driver
from usage import UsageStore

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, 'web')
NICKNAMES_FILE = os.path.join(HERE, 'nicknames.json')
USAGE_FILE = os.path.join(HERE, 'usage.db')
SAMPLE_EVERY = 60    # seconds between usage samples
CONFIG_FILE = os.path.join(HERE, 'config.json')
DEFAULT_CONFIG = {
    'router': '192.168.1.1',
    'driver': 'realtek-boa',
    'protect': {},         # {ip: reason} for devices the panel must never block or limit
    'interfaceNames': {},  # {"LAN1": "Upstairs access point"} to label wired ports
}
MIN_LIMIT_KBPS = 256
MAX_LIMIT_KBPS = 1000000
STATIC_TYPES = {'.html', '.css', '.js', '.svg', '.png', '.ico', '.woff2'}
CGNAT = ipaddress.ip_network('100.64.0.0/10')


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            config.update(json.load(f))
    except FileNotFoundError:
        pass
    except ValueError as e:
        raise SystemExit('config.json is not valid JSON: %s' % e)
    return config


# --- Device names from the router's DNS (most home routers register DHCP host names) ---

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


def lookup_hostname(ip, dns_server):
    qname = '.'.join(reversed(ip.split('.'))) + '.in-addr.arpa'
    tid = random.randint(0, 0xFFFF)
    query = struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0)
    query += b''.join(bytes([len(p)]) + p.encode('ascii') for p in qname.split('.')) + b'\0'
    query += struct.pack('>HH', 12, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.6)
            s.sendto(query, (dns_server, 53))
            data = s.recv(512)
        if struct.unpack('>H', data[:2])[0] != tid or struct.unpack('>H', data[6:8])[0] == 0:
            return None
        offset = _skip_name(data, _skip_name(data, 12) + 4)
        rtype = struct.unpack('>H', data[offset:offset + 2])[0]
        return _read_name(data, offset + 10).split('.')[0] or None if rtype == 12 else None
    except (OSError, struct.error, IndexError):
        return None


def own_ip(router_ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((router_ip, 53))
            return s.getsockname()[0]
    except OSError:
        return None


def _mac(value):
    value = (value or '').strip().lower()
    digits = re.sub(r'[^0-9a-f]', '', value)
    if len(digits) != 12 or not re.fullmatch(r'[0-9a-f]{2}([:-]?)[0-9a-f]{2}(\1[0-9a-f]{2}){4}', value):
        return None
    return ':'.join(digits[i:i + 2] for i in range(0, 12, 2))


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _seconds(text):
    """'3 days, 3:18', 'up 3days,03:18:27 / ...' or '45 min' -> seconds."""
    text = (text or '').split('/')[0]
    days = re.search(r'(\d+)\s*days?', text)
    clock = re.search(r'(\d+):(\d+)(?::(\d+))?', text)
    mins = re.search(r'(\d+)\s*min', text)
    total = int(days.group(1)) * 86400 if days else 0
    if clock:
        total += int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + int(clock.group(3) or 0)
    elif mins:
        total += int(mins.group(1)) * 60
    return total or None


def _address_kind(ip):
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.version == 4 and address in CGNAT:
        return 'cgnat'
    if address.is_private:
        return 'private'
    return 'public'


def _fold_old_addresses(devices):
    """Tidy the device list.

    Phones with private MACs switch to a new random MAC now and then. The router then
    hands out a new lease and keeps the old one (same host name, old MAC) for up to a day,
    so one phone shows up several times. Fold those offline entries into the device that
    is online under that name. Entries with no name, no lease and no recent traffic are
    leftovers from the ARP table; mark them stale so the page can tuck them away.
    """
    by_name = {}
    for d in devices:
        if d['hostname']:
            by_name.setdefault(d['hostname'], []).append(d)
    folded = set()
    for group in by_name.values():
        if len(group) < 2:
            continue
        online = [d for d in group if d['online']]
        keep = online or [max(group, key=lambda d: d['lease'] or 0)]
        keep_ids = {id(d) for d in keep}
        primary = max(keep, key=lambda d: (bool(d['wifi']), d['lease'] or 0))
        for d in group:
            # Never hide a device that is online (it may be a second phone of the same model),
            # or one with a block, limit or nickname (the user needs its card).
            if id(d) in keep_ids or d['blocked'] or d['limit']['down'] or d['limit']['up'] or d['nickname']:
                continue
            primary.setdefault('olderAddresses', []).append({'ip': d['ip'], 'mac': d['mac']})
            folded.add(d['mac'])
    out = []
    for d in devices:
        if d['mac'] in folded:
            continue
        d.setdefault('olderAddresses', [])
        d['stale'] = not (d['online'] or d['lease'] or d['hostname'] or d['nickname'] or d['blocked'])
        out.append(d)
    return out


# --- Panel state and actions ---

class Panel:
    def __init__(self, driver, config):
        self.router = driver
        self.config = config
        self.lock = threading.RLock()
        self.cache = {}       # key -> (fetched at, value)
        self.hostnames = {}   # ip -> (name, looked up at)
        self.samples = {}     # mac -> (time, txBytes, rxBytes) for live speeds
        self.own_ip = own_ip(driver.host)
        self.diag = {'kind': None, 'host': None, 'started': 0, 'lines': [], 'done': True, 'same': 0}
        self.usage = UsageStore(USAGE_FILE) if 'usage' in driver.capabilities else None
        self.live = []          # [(time, down bit/s, up bit/s)] for the live graph, newest last
        self.live_prev = None   # (time, down counter, up counter)
        self.optics_at = 0
        try:
            with open(NICKNAMES_FILE, encoding='utf-8') as f:
                self.nicknames = json.load(f)
        except (OSError, ValueError):
            self.nicknames = {}

    def _cached(self, key, ttl, fetch, force=False):
        with self.lock:
            hit = self.cache.get(key)
            if not force and hit and time.time() - hit[0] < ttl:
                return hit[1]
            value = fetch()
            self.cache[key] = (time.time(), value)
            return value

    def _forget(self, *keys):
        with self.lock:
            for key in keys:
                self.cache.pop(key, None)

    # --- Devices ---

    def _clients(self, force=False):
        def fetch():
            clients = self.router.clients()
            self._measure_speeds(clients['stations'])
            ips = {v['ip'] for v in clients['dhcp'].values()} | set(clients['arp'].values())
            now = time.time()
            for ip in ips:
                if ip not in self.hostnames or now - self.hostnames[ip][1] > 600:
                    self.hostnames[ip] = (lookup_hostname(ip, self.router.host), now)
            return clients
        return self._cached('clients', 4, fetch, force)

    def _rules(self, force=False):
        return self._cached('rules', 30, lambda: {'blocks': self.router.blocks(), 'limits': self.router.limits()}, force)

    def _status(self, force=False):
        return self._cached('status', 30, self.router.status, force)

    def _measure_speeds(self, stations):
        now = time.time()
        for radio in stations.values():
            for mac, st in radio.items():
                prev = self.samples.get(mac)
                if st['txBytes'] is None or st['rxBytes'] is None:
                    continue
                self.samples[mac] = (now, st['txBytes'], st['rxBytes'])
                if prev and now - prev[0] > 1 and st['txBytes'] >= prev[1] and st['rxBytes'] >= prev[2]:
                    seconds = now - prev[0]
                    st['downKbps'] = round((st['txBytes'] - prev[1]) * 8 / seconds / 1000)
                    st['upKbps'] = round((st['rxBytes'] - prev[2]) * 8 / seconds / 1000)

    def _protected_macs(self, clients):
        """{mac: reason} for every protected device, resolved to its MAC.

        Blocking and limiting act on the MAC, so protection must too: a protected device
        must stay protected even when its IP is momentarily missing from the lease/ARP
        tables or has changed since config was written. config['protect'] keys may be IPs
        (resolved here against the current tables) or MACs (used directly).
        """
        ip_to_mac = {v['ip']: m for m, v in clients['dhcp'].items() if v.get('ip')}
        ip_to_mac.update({ip: m for m, ip in clients['arp'].items()})
        out = {}
        for key, reason in self.config['protect'].items():
            as_mac = _mac(key)
            if as_mac:
                out[as_mac] = reason
            elif key in ip_to_mac:
                out[ip_to_mac[key]] = reason
        if self.own_ip and self.own_ip in ip_to_mac:
            out.setdefault(ip_to_mac[self.own_ip], 'This computer runs the panel')
        return out

    def _protected(self, mac, ip, client_ip, protected_macs):
        if ip and ip == self.router.host:
            return 'The router itself'
        if mac in protected_macs:
            return protected_macs[mac]
        if ip and ip in self.config['protect']:
            return self.config['protect'][ip]
        if ip and ip == self.own_ip:
            return 'This computer runs the panel'
        if ip and ip == client_ip:
            return "The device you're using right now"
        return None

    def _interfaces(self, clients):
        names = self.config['interfaceNames']
        found = []
        for radio_id, radio in sorted(clients['radios'].items(), key=lambda r: r[1]['band'], reverse=True):
            found.append({'id': radio_id, 'kind': 'wifi', 'band': radio['band'],
                          'label': names.get(radio_id) or '%s GHz Wi-Fi' % radio['band']})
        for port in sorted(set(clients['ports'].values()) - set(clients['radios'])):
            found.append({'id': port, 'kind': 'lan', 'band': None, 'label': names.get(port) or port + ' (cable)'})
        return found

    def _devices(self, clients, rules, client_ip):
        macs = list(clients['dhcp'])
        stations = clients['stations']
        sources = [clients['arp']] + list(stations.values()) + [[r['src'] for r in rules['blocks']]]
        for source in sources:
            macs += [m for m in source if m and m not in macs]
        protected_macs = self._protected_macs(clients)
        devices = []
        for mac in macs:
            lease = clients['dhcp'].get(mac) or {}
            ip = lease.get('ip') or clients['arp'].get(mac)
            radio = next((radio_id for radio_id, found in stations.items() if mac in found), None)
            wifi = stations[radio][mac] if radio else None
            limits = [r for r in rules['limits'] if ip and ip in (r['srcip'], r['dstip'])]
            devices.append({
                'mac': mac,
                'ip': ip,
                'hostname': self.hostnames.get(ip, (None,))[0] if ip else None,
                'nickname': self.nicknames.get(mac),
                'iface': radio or clients['ports'].get(mac),
                # Online = associated to a radio now, or seen in the bridge table (frames in the last
                # few minutes). ARP alone isn't enough: entries outlive the device by a long time.
                'online': bool(wifi) or mac in clients['ports'],
                'privateMac': bool(int(mac[:2], 16) & 2),
                'lease': lease.get('lease'),
                'wifi': wifi,
                'blocked': any(r['src'] == mac and r['action'].lower() == 'deny' for r in rules['blocks']),
                'limit': {
                    'down': next((r['rate'] for r in limits if r['direction'] == 'down'), None),
                    'up': next((r['rate'] for r in limits if r['direction'] == 'up'), None),
                },
                'protected': self._protected(mac, ip, client_ip, protected_macs),
            })
        return _fold_old_addresses(devices)

    def state(self, client_ip):
        with self.lock:  # read the cache timestamp atomically with the fetch (login() can clear the cache)
            clients, rules = self._clients(), self._rules()
            updated = int(self.cache['clients'][0])
            status = self._status()
        return {
            'router': status,
            'family': self.router.family,
            'capabilities': sorted(self.router.capabilities),
            'interfaces': self._interfaces(clients),
            'devices': self._devices(clients, rules, client_ip),
            'updated': updated,
            'minLimitKbps': MIN_LIMIT_KBPS,
        }

    # --- Usage recording ---

    def start_recorder(self):
        """Sample the router's counters every minute in the background (also keeps the session alive)."""
        if not self.usage:
            return
        def loop():
            pruned = 0
            while True:
                try:
                    self._sample()
                    if time.time() - pruned > 86400:
                        self.usage.prune(time.time())
                        pruned = time.time()
                except (NotLoggedIn, RouterError, OSError):
                    pass  # logged out or unreachable: this minute is simply not recorded
                except Exception as e:  # never let a bad sample kill the recorder
                    print('usage sample failed:', e)
                time.sleep(SAMPLE_EVERY)
        threading.Thread(target=loop, name='usage-recorder', daemon=True).start()

    def _sample(self):
        now = time.time()
        wan = self.router.counters()
        clients = self._clients()
        stations, names = {}, {}
        for radio_id, found in clients['stations'].items():
            band = clients['radios'].get(radio_id, {}).get('band')
            for mac, st in found.items():
                if st['txBytes'] is not None and st['rxBytes'] is not None:
                    stations[mac] = (st['txBytes'], st['rxBytes'])  # the AP's TX is the device's download
                ip = (clients['dhcp'].get(mac) or {}).get('ip') or clients['arp'].get(mac)
                name = self.nicknames.get(mac) or (self.hostnames.get(ip, (None,))[0] if ip else None)
                names[mac] = (name, ip, band)
        optics = None
        if 'fibre' in self.router.capabilities and now - self.optics_at > 300:
            optics = self.router.optics()
            self.optics_at = now
        self.usage.record(now, wan, stations, names, optics)

    def stats(self, client_ip):
        if not self.usage:
            raise ValueError('This router has no usage counters')
        if self.usage.boot_totals is None:
            self.usage.boot_totals = self.router.counters()
        out = self.usage.summary()
        boot = self.usage.boot_totals
        out['sinceBoot'] = {'down': boot[0], 'up': boot[1]} if boot else None
        out['uptimeSeconds'] = _seconds(self._status().get('uptime'))
        out['sampleEvery'] = SAMPLE_EVERY
        return out

    def live_rate(self, client_ip):
        """Whole-connection speed right now, from two reads of the byte counters."""
        if not self.usage:
            raise ValueError('This router has no usage counters')
        with self.lock:
            now = time.time()
            if not self.live_prev or now - self.live_prev[0] >= 1.5:  # several open pages share one read
                counters = self.router.counters()
                if counters:
                    prev = self.live_prev
                    self.live_prev = (now, counters[0], counters[1])
                    if prev and counters[0] >= prev[1] and counters[1] >= prev[2]:
                        secs = now - prev[0]
                        self.live.append((now, (counters[0] - prev[1]) * 8 / secs, (counters[1] - prev[2]) * 8 / secs))
                        del self.live[:-200]
            return {'points': [{'t': t, 'down': d, 'up': u} for t, d, u in self.live]}

    # --- Internet, Wi-Fi and security views (read-only) ---

    def internet(self, client_ip):
        status = self._status()
        info = self._cached('internet', 30, self.router.internet)
        wan = dict(status.get('wan') or {})
        wan['addressKind'] = _address_kind(wan.get('ip'))
        wan['upSeconds'] = _seconds(wan.get('status'))
        uptime = _seconds(status.get('uptime'))
        fibre = info.get('fibre')
        return {'router': status, 'wan': wan, 'uptimeSeconds': uptime, 'ipv6': info['ipv6'], 'fibre': fibre,
                'ports': info['ports'], 'interfaces': info['interfaces'], 'dns': status.get('dns') or []}

    def wifi(self, client_ip):
        radios = self._cached('radios', 120, self.router.radios)
        neighbours = self._cached('neighbours', 300, self.router.neighbours)
        clients = self._clients()
        counts = {radio_id: len(found) for radio_id, found in clients['stations'].items()}
        # An access point on your LAN (e.g. a second router in AP mode) usually beacons with
        # the same MAC it uses for its DHCP lease, so a neighbour BSSID with a lease is yours.
        yours = {r['bssid'] for r in radios} | set(clients['dhcp'])
        for n in neighbours:
            n['yours'] = n['bssid'] in yours
        return {'radios': [dict(r, clients=counts.get(r['id'], 0)) for r in radios], 'neighbours': neighbours}

    def security(self, client_ip):
        settings = self._cached('security', 120, self.router.security)
        radios = self._cached('radios', 120, self.router.radios)
        status = self._status()
        return {'checks': security_checks(settings, radios, status)}

    # --- Diagnostics ---

    def diag_start(self, body, client_ip):
        kind, host = str(body.get('kind') or ''), str(body.get('host') or '').strip()
        if kind not in ('ping', 'traceroute') or kind not in self.router.capabilities:
            raise ValueError('Unknown diagnostic')
        with self.lock:
            if not self.diag['done'] and time.time() - self.diag['started'] < 90:
                raise ValueError('A %s is still running. Wait for it to finish.' % self.diag['kind'])
            self.router.start_diag(kind, host)
            self.diag = {'kind': kind, 'host': host, 'started': time.time(), 'lines': [], 'done': False, 'same': 0}
        return {'ok': True}

    def diag_poll(self, client_ip):
        with self.lock:
            d = self.diag
            if d['kind'] and not d['done']:
                lines = self.router.diag_output(d['kind'])
                d['same'] = d['same'] + 1 if lines == d['lines'] and lines else 0
                d['lines'] = lines
                text = '\n'.join(lines).lower()
                if d['kind'] == 'ping':
                    finished = 'packet loss' in text
                else:
                    # "traceroute to 1.1.1.1 (1.1.1.1), ..." then one line per hop; done when the target answers
                    target = re.search(r'traceroute to \S+ \(([^)]+)\)', text)
                    last = lines[-1].lower() if len(lines) > 1 else ''
                    finished = bool(target and '(%s)' % target.group(1) in last) or d['same'] >= 6
                if finished or time.time() - d['started'] > 90:
                    d['done'] = True
            return {k: d[k] for k in ('kind', 'host', 'lines', 'done', 'started')}

    # --- Changes ---

    def _find(self, body, client_ip):
        mac = _mac(body.get('mac'))
        if not mac:
            raise ValueError('Missing or invalid MAC address')
        clients, rules = self._clients(force=True), self._rules(force=True)
        device = next((d for d in self._devices(clients, rules, client_ip) if d['mac'] == mac), None)
        return mac, device, rules

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
                self.router.block(mac)
            rules = self._rules(force=True)
            if not any(r['src'] == mac and r['action'].lower() == 'deny' for r in rules['blocks']):
                raise RouterError('The router did not save the block rule')
            return {'ok': True, 'message': '%s is blocked' % self._label(device, mac)}

    def unblock(self, body, client_ip):
        with self.lock:
            mac, device, rules = self._find(body, client_ip)
            mine = [r for r in rules['blocks'] if r['src'] == mac]
            if mine:
                self.router.unblock(mine)
                if any(r['src'] == mac for r in self._rules(force=True)['blocks']):
                    raise RouterError('The router did not remove the block rule')
            return {'ok': True, 'message': '%s is unblocked' % self._label(device, mac)}

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
            mac, device, rules = self._find(body, client_ip)
            if not device or not device['ip']:
                raise ValueError('That device has no IP address right now')
            if device['protected']:
                raise ValueError('Protected: ' + device['protected'])
            ip = device['ip']
            ids = [r['id'] for r in rules['limits'] if ip in (r['srcip'], r['dstip'])]
            if ids:
                self.router.remove_limits(ids)
            for direction, rate in (('down', down), ('up', up)):
                if rate:
                    self.router.add_limit(ip, rate, direction)
            rules = self._rules(force=True)
            saved = {(r['direction'], r['rate']) for r in rules['limits'] if ip in (r['srcip'], r['dstip'])}
            wanted = {('down', down), ('up', up)} - {('down', None), ('up', None)}
            if not wanted <= saved:
                raise RouterError('The router did not save the speed limit')
            return {'ok': True, 'message': 'Speed limit set for %s' % self._label(device, mac)}

    def unlimit(self, body, client_ip):
        with self.lock:
            mac, device, rules = self._find(body, client_ip)
            ip = device and device['ip']
            ids = [r['id'] for r in rules['limits'] if ip and ip in (r['srcip'], r['dstip'])]
            if ids:
                self.router.remove_limits(ids)
                if any(ip in (r['srcip'], r['dstip']) for r in self._rules(force=True)['limits']):
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
            with open(NICKNAMES_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.nicknames, f, indent=2)
        return {'ok': True}

    def login(self, body, client_ip):
        username, password = str(body.get('username') or ''), str(body.get('password') or '')
        if not username or not password:
            raise ValueError('Enter the router username and password')
        self.router.login(username, password)
        with self.lock:
            self.cache.clear()
        return {'ok': True}

    def logout(self, body, client_ip):
        with self.lock:
            self.router.logout()
            self.cache.clear()
        return {'ok': True, 'message': 'Logged out of the router'}


def security_checks(s, radios, status):
    """Turn raw settings into a check-up list: {id, level: good|info|warn|bad, title, detail}."""
    checks = []

    def add(check_id, level, title, detail):
        checks.append({'id': check_id, 'level': level, 'title': title, 'detail': detail})

    def bands(items):
        return ' and '.join('%s GHz' % r['band'] for r in items)

    # A disabled radio's WPS/encryption/PMF settings don't matter — judge only the active ones.
    active = [r for r in radios if r.get('enabled')]

    wps_on = [r for r in active if r['wps']['enabled']]
    if wps_on and any(r['wps']['defaultPin'] for r in wps_on):
        add('wps', 'bad', 'WPS is on, with the factory PIN',
            'WPS is enabled on %s and the router PIN is %s, a default that many routers of this chipset share.'
            % (bands(wps_on), wps_on[0]['wps']['pin']))
    elif wps_on:
        add('wps', 'warn', 'WPS is on', 'WPS is enabled on %s.' % bands(wps_on))
    else:
        add('wps', 'good', 'WPS is off', 'Neither radio accepts WPS PIN or push-button pairing.')

    weak = [r for r in active if r['security'] not in ('WPA2', 'WPA3', 'WPA2/WPA3 transition')
            or (r.get('cipher') or '').startswith('TKIP')]
    if weak:
        add('wifi-encryption', 'bad', 'Weak Wi-Fi encryption',
            ', '.join('%s GHz uses %s %s' % (r['band'], r['security'], r.get('cipher') or '') for r in weak))
    elif active:
        add('wifi-encryption', 'good', 'Wi-Fi encryption is strong',
            ', '.join('%s GHz: %s-%s, %s' % (r['band'], r['security'], 'PSK' if 'PSK' in (r.get('auth') or '')
                                            else 'Enterprise', r.get('cipher')) for r in active))

    no_pmf = [r for r in active if r.get('pmf') == 'off']
    if no_pmf:
        add('pmf', 'warn', 'Management frames are unprotected',
            '802.11w (Protected Management Frames) is off on %s.' % bands(no_pmf))

    wan_rules = [r for r in s['acl']['rules'] if r['enabled'] and r['side'].upper() == 'WAN']
    exposed = [r for r in wan_rules if r['services'].lower() not in ('ping', 'icmp', '')]
    if not s['acl']['enabled']:
        add('remote-admin', 'warn', 'Admin access list is off',
            'The router does not restrict which addresses can open its management services.')
    elif exposed:
        add('remote-admin', 'bad', 'Router admin is reachable from the internet',
            'WAN-side access list allows: %s.' % ', '.join(r['services'] for r in exposed))
    else:
        add('remote-admin', 'good', 'Router admin is LAN-only',
            'From the internet side the router only answers %s.' % (', '.join(r['services'] for r in wan_rules) or 'nothing'))

    add('upnp', 'good' if not s['upnp'] else 'info', 'UPnP is %s' % ('on' if s['upnp'] else 'off'),
        'Devices %s open ports on the router by themselves.' % ('can' if s['upnp'] else 'cannot'))

    if s['dmz']['enabled']:
        add('dmz', 'warn', 'DMZ host is set', 'All unsolicited inbound IPv4 goes to %s.' % s['dmz']['host'])
    else:
        add('dmz', 'good', 'No DMZ host', 'Unsolicited inbound IPv4 is not sent to any device.')

    forwards = s['portForwarding']['rules']
    if forwards:
        add('port-forwarding', 'info', '%d port forward%s' % (len(forwards), '' if len(forwards) == 1 else 's'),
            ', '.join('%s %s -> %s:%s' % (f['protocol'], f.get('publicPort') or f['localPort'], f['localIp'],
                                          f['localPort']) for f in forwards))
    else:
        add('port-forwarding', 'good', 'No port forwards', 'No inbound ports are mapped to devices.')

    if s['ipv6']:
        if s['ipv6Filter']['incoming'] == 'deny':
            add('ipv6-inbound', 'good', 'IPv6 inbound is blocked',
                'Your devices have public IPv6 addresses, and the IPv6 firewall drops unsolicited inbound traffic.')
        else:
            add('ipv6-inbound', 'bad', 'IPv6 inbound is allowed',
                'Your devices have public IPv6 addresses and the IPv6 firewall lets unsolicited inbound traffic in.')

    wan = status.get('wan') or {}
    kind = _address_kind(wan.get('ip'))
    if kind in ('cgnat', 'private'):
        add('cgnat', 'info', 'Behind carrier-grade NAT',
            'The router\'s WAN IPv4 is %s, which is not a public address.' % wan.get('ip'))
    if 'TR069' in (wan.get('type') or '').upper():
        add('tr069', 'info', 'The ISP can manage this router',
            'The WAN connection type is %s, so the ISP\'s TR-069 server can read and change settings.' % wan.get('type'))

    build = re.search(r'e(\d{2})(\d{2})(\d{2})', status.get('firmware') or '')
    add('firmware', 'info', 'Firmware %s' % (status.get('firmware') or 'unknown'),
        'Built 20%s-%s-%s. Firmware is pushed by the ISP; there is no self-update.' % build.groups()
        if build else 'Firmware is pushed by the ISP.')

    guests = sum(r['guestNetworks']['used'] for r in radios)
    slots = sum(r['guestNetworks']['slots'] for r in radios)
    if slots:
        add('guest', 'info', 'Guest networks: %d of %d in use' % (guests, slots),
            'Each radio can broadcast up to %d extra SSIDs.' % radios[0]['guestNetworks']['slots'])

    order = {'bad': 0, 'warn': 1, 'info': 2, 'good': 3}
    checks.sort(key=lambda c: order[c['level']])
    return checks


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
        self.send_header('X-Content-Type-Options', 'nosniff')
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

    def _static(self, path):
        name = 'index.html' if path in ('/', '') else path.lstrip('/')
        full = os.path.join(WEB, name)
        if '/' in name or '\\' in name or os.path.splitext(name)[1] not in STATIC_TYPES or not os.path.isfile(full):
            return self._json(404, {'error': 'Not found'})
        with open(full, 'rb') as f:
            body = f.read()
        kind = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        if kind.startswith('text/') or kind == 'application/javascript':
            kind += '; charset=utf-8'
        self._send(200, body, kind)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        views = {
            '/api/state': self.panel.state,
            '/api/internet': self.panel.internet,
            '/api/wifi': self.panel.wifi,
            '/api/security': self.panel.security,
            '/api/diag': self.panel.diag_poll,
            '/api/stats': self.panel.stats,
            '/api/live': self.panel.live_rate,
        }
        if path in views:
            if self._allowed(api=True):
                self._call(views[path], self.client_address[0])
        elif path.startswith('/api/'):
            self._json(404, {'error': 'Not found'})
        elif self._allowed(api=False):
            self._static(path)

    def do_POST(self):
        actions = {
            '/api/block': self.panel.block,
            '/api/unblock': self.panel.unblock,
            '/api/limit': self.panel.limit,
            '/api/unlimit': self.panel.unlimit,
            '/api/name': self.panel.rename,
            '/api/login': self.panel.login,
            '/api/logout': self.panel.logout,
            '/api/diag': self.panel.diag_start,
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
    parser = argparse.ArgumentParser(description='fun-router: a fun, explained console for your router')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--lan', action='store_true', help='also listen on the Wi-Fi so phones can use it')
    parser.add_argument('--pin', help='PIN that other devices must enter (required with --lan)')
    args = parser.parse_args()
    if args.lan and not args.pin:
        parser.error('--lan needs --pin, otherwise anyone on your Wi-Fi could block devices')

    config = load_config()
    mimetypes.add_type('application/javascript', '.js')
    Handler.panel = Panel(make_driver(config['driver'], config['router']), config)
    Handler.panel.start_recorder()
    Handler.pin = args.pin
    bind = '0.0.0.0' if args.lan else '127.0.0.1'
    lan_ip = Handler.panel.own_ip
    if args.lan and lan_ip:
        Handler.hosts = Handler.hosts | {lan_ip}
    server = http.server.ThreadingHTTPServer((bind, args.port), Handler)
    print('fun-router: http://127.0.0.1:%d  (router %s, %s)' % (args.port, config['router'], config['driver']))
    if args.lan and lan_ip:
        print('On your Wi-Fi:  http://%s:%d  (PIN required)' % (lan_ip, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
