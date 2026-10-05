"""Driver for Realtek "Boa" GPON ONTs, such as the OVT OP2200H that GTPL installs.

The console is server-rendered ASP pages (read with GET) plus /boaform/ form posts
(writes). Facts this driver relies on, found on firmware V4.0.1--e240304:

- Login is tied to the client IP, not cookies. When it lapses, every page returns a bare
  "You have not logined" page with no HTTP status line.
- The web server answers one request at a time, so all calls go through one lock.
- Every POST carries postSecurityFlag, a 16-bit checksum of the URL-encoded body
  (postTableEncrypt in /common.js). Field order must match the page's DOM order.
- Wi-Fi pages are per radio: /boaform/formWlanRedirect stores the radio index in the
  session, then the page is fetched.
- Rows often have no </tr>; base.rows() copes with that.
"""

import base64
import http.client
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import base
from .base import NotLoggedIn, RouterError

WAN_IFACE = '65536'  # ppp0_nas0_0, the only WAN in net_qos_traffictl_edit.asp
RADIOS = ('wlan0', 'wlan1')  # wlan0 = 5 GHz, wlan1 = 2.4 GHz on the OP2200H; read from the router anyway

# The only router forms this driver can submit, and the only submit buttons it may press
# on them. Firmware, backup/restore, reboot, WAN/GPON/TR-069, passwords, Wi-Fi settings,
# the MAC filter's default action and its "Delete All" button are deliberately unreachable.
ALLOWED_FORMS = {
    '/boaform/admin/formLogin': {'save'},
    '/boaform/admin/formFilter': {'addFilterMac', 'deleteSelFilterMac'},
    '/boaform/admin/formQosTraffictlEdit': set(),
    '/boaform/admin/formQosTraffictl': set(),
    '/boaform/formPing': set(),
    '/boaform/formTracert': {'go'},
}
# Posts with an empty body that only read the output of a running diagnostic.
RESULT_POSTS = {'/boaform/formPingResult', '/boaform/formTracertResult'}
BUTTON_FIELDS = {'save', 'addFilterMac', 'deleteSelFilterMac', 'setMacDft', 'deleteAllFilterMac', 'go'}
FORBIDDEN_FIELDS = {'setMacDft', 'deleteAllFilterMac', 'outAct', 'inAct'}
# Same rule as the console's own ping/traceroute pages. It also keeps shell metacharacters
# away from the router, which passes the host to its ping and traceroute commands.
# \Z (not $) so a trailing newline can't slip a %0A into the command the router runs.
HOST = re.compile(r'\A(?=.{1,253}\Z)([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9])'
                  r'(\.([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]))*\Z')


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
    values = dict(fields)
    if action == '/boaform/admin/formQosTraffictl' and not values.get('lst', '').startswith('applysetting#id='):
        raise RouterError('Blocked: unexpected Traffic Shaping request')
    if action == '/boaform/formPing' and (values.get('pingAct') != 'Start' or not HOST.match(values.get('pingAddr', ''))):
        raise RouterError('Blocked: unexpected ping request')
    if action == '/boaform/formTracert' and (values.get('tracertAct') != 'Start'
                                             or not HOST.match(values.get('traceAddr', ''))):
        raise RouterError('Blocked: unexpected traceroute request')


# --- Page parsers ---

def parse_status(page):
    info = base.pairs(page)
    wan = None
    for _, cells in base.rows(page):
        if len(cells) >= 7 and re.match(r'(ppp|nas|eth|vlan)', cells[0]):
            wan = {'iface': cells[0], 'vlan': cells[1], 'type': cells[2], 'protocol': cells[3],
                   'ip': cells[4], 'gateway': cells[5], 'status': cells[6],
                   'up': cells[6].lower().startswith('up')}
            break
    dns = [s.strip() for s in (info.get('Name Servers') or '').split(',') if s.strip()]
    lan_mac = base.mac(info.get('MAC Address') or '')
    return {
        'model': info.get('Device Name'),
        'firmware': info.get('Firmware Version'),
        'uptime': info.get('Uptime'),
        'cpu': info.get('CPU Usage'),
        'memory': info.get('Memory Usage'),
        'dns': dns,
        'lan': {'ip': info.get('IP Address'), 'mask': info.get('Subnet Mask'), 'mac': lan_mac,
                'dhcp': info.get('DHCP Server')},
        'wan': wan,
    }


def parse_ipv6(page):
    info = base.pairs(page)
    wan = None
    for _, cells in base.rows(page):
        if len(cells) >= 6 and re.match(r'(ppp|nas|eth|vlan)', cells[0]):
            wan = {'iface': cells[0], 'protocol': cells[3], 'ip': cells[4], 'status': cells[5]}
            break
    return {'lanAddress': info.get('IPv6 Address'), 'linkLocal': info.get('IPv6 Link-Local Address'),
            'prefix': info.get('Prefix'), 'wan': wan}


def parse_dhcp(page):
    leases = {}
    for _, cells in base.rows(page):
        if len(cells) >= 3 and base.IP.match(cells[0]) and base.mac(cells[1]):
            leases[base.mac(cells[1])] = {'ip': cells[0], 'lease': base.to_int(cells[2])}
    return leases


def parse_arp(page):
    return {base.mac(c[1]): c[0] for _, c in base.rows(page) if len(c) >= 2 and base.IP.match(c[0]) and base.mac(c[1])}


def parse_fdb(page):
    """{mac: (switch port, is the router's own MAC)} from the bridge forwarding database."""
    return {base.mac(c[1]): (base.to_int(c[0]), c[2].lower() == 'yes')
            for _, c in base.rows(page) if len(c) >= 3 and base.mac(c[1]) and c[0].isdigit()}


def parse_stations(page):
    stations = {}
    for _, c in base.rows(page):
        if len(c) >= 14 and base.mac(c[0]):
            stations[base.mac(c[0])] = {
                'linkMbps': base.to_int(c[1]),
                'rxRateMbps': base.to_int(c[2]),
                'txBytes': base.to_int(c[5]),
                'rxBytes': base.to_int(c[6]),
                'rssi': base.to_int(c[7]),
                'mcs': c[10] if len(c) > 10 and c[10] not in ('N/A', '---') else None,
                'antennas': c[12] if len(c) > 12 else None,
                'uptime': base.to_int(c[13]),
                'powerSave': len(c) > 14 and c[14].lower() == 'yes',
            }
    return stations


def parse_mac_rules(page):
    start = page.find('name="formFilterDel"')
    section = _form_section(page, start)
    rules = []
    for row, cells in base.rows(section):
        box = re.search(r'<input[^>]*type=["\']?checkbox[^>]*>', row, re.I)
        if not box or len(cells) < 5:
            continue
        name = re.search(r'name=["\']?([\w\[\]]+)', box.group(0))
        value = re.search(r'value=["\']?([^"\'\s>]+)', box.group(0))
        rules.append({
            'field': name.group(1) if name else None,
            'value': value.group(1) if value else 'on',
            'direction': cells[1],
            'src': base.mac(cells[2]),
            'action': cells[4],
        })
    return rules


_SHAPING_RULE = re.compile(r'traffictlRules(?:\.push\(|\[\d+\]\s*=)(.*?)\)\s*;', re.S)
_SHAPING_PAIR = re.compile(r'new it\(\s*"(\w+)"\s*,\s*(?:"([^"]*)"|([^)\s]*))\s*\)|(\w+)\s*:\s*(?:"([^"]*)"|([^,}\s]*))')


def parse_shaping(page):
    rules = []
    for match in _SHAPING_RULE.finditer(page):
        found = {}
        for g in _SHAPING_PAIR.findall(match.group(1)):
            key, value = (g[0], g[1] or g[2]) if g[0] else (g[3], g[4] or g[5])
            found[key] = value.strip()
        if 'rate' in found:
            rules.append({
                'id': found.get('id'),
                'srcip': found.get('srcip'),
                'dstip': found.get('dstip'),
                'rate': base.to_int(found['rate']),
                'direction': 'down' if found.get('direction') == '1' else 'up',
            })
    return rules


def parse_fibre(status_page, stats_page):
    s = base.pairs(status_page)
    t = base.pairs(stats_page)
    return {
        'vendor': s.get('Vendor Name'), 'part': s.get('Part Number'),
        'rxDbm': base.number(s.get('Rx Power')), 'txDbm': base.number(s.get('Tx Power')),
        'temperatureC': base.number(s.get('Temperature')), 'voltage': base.number(s.get('Voltage')),
        'biasMa': base.number(s.get('Bias Current')), 'onuState': s.get('ONU State'),
        'bytesIn': base.to_int(t.get('Bytes Received')), 'bytesOut': base.to_int(t.get('Bytes Sent')),
        'fecErrors': base.to_int(t.get('FEC Errors')), 'hecErrors': base.to_int(t.get('HEC Errors')),
        'dropped': base.to_int(t.get('Packets Dropped')),
    }


def parse_ports(page):
    ports = []
    for _, c in base.rows(page):
        if len(c) == 2 and re.match(r'LAN\d', c[0]):
            parts = [p.strip() for p in c[1].split(',')]
            up = parts[0].lower() == 'up'
            ports.append({'name': c[0], 'up': up, 'speed': parts[1] if up and len(parts) > 1 else None,
                          'duplex': parts[2] if up and len(parts) > 2 else None})
    return ports


def parse_interfaces(page):
    out = []
    for _, c in base.rows(page):
        if len(c) >= 7 and base.to_int(c[1]) is not None:
            out.append({'name': c[0], 'rxPackets': base.to_int(c[1]), 'rxErrors': base.to_int(c[2]),
                        'rxDrops': base.to_int(c[3]), 'txPackets': base.to_int(c[4]),
                        'txErrors': base.to_int(c[5]), 'txDrops': base.to_int(c[6])})
    return out


def _js_values(page, name):
    """Values the page script assigns to NAME, e.g. channel_drv[0]='44'; -> ['44']."""
    return [a or b for a, b in re.findall(r'\b%s(?:\[\d+\])?\s*=\s*(?:\'([^\']*)\'|"?([\w.:-]*)"?)[ \t]*(?:;|$)'
                                          % re.escape(name), page, re.M)]


def _js_first(page, name):
    values = _js_values(page, name)
    return values[0] if values else None


def _choice(page, name):
    """Current value of a radio-button group: the last top-level script assignment
    (document.form.NAME[i].checked = true) wins, otherwise the checked attribute."""
    group = [i for i in base.inputs(page) if i.get('name') == name and i.get('type', '').lower() == 'radio']
    picks = re.findall(r'^\s*document\.\w+\.%s\[(\d+)\]\.checked\s*=\s*true' % re.escape(name), page, re.M)
    if picks and int(picks[-1]) < len(group):
        return group[int(picks[-1])].get('value')
    checked = [i.get('value') for i in group if 'checked' in i]
    return checked[0] if checked else None


def _field(page, name):
    found = [i for i in base.inputs(page) if i.get('name') == name]
    return found[0] if found else {}


def _select_js(page, form_field, prop='value'):
    match = re.findall(r'document\.\w+\.%s\.%s\s*=\s*(\d+)' % (re.escape(form_field), prop), page)
    return int(match[-1]) if match else None


_BAND_BITS = ((1, 'b'), (2, 'g'), (4, 'a'), (8, 'n'), (64, 'ac'), (128, 'ax'))
_SECURITY = {'0': 'Open', '1': 'WEP', '2': 'WPA', '4': 'WPA2', '6': 'WPA/WPA2 mixed', '16': 'WPA3',
             '20': 'WPA2/WPA3 transition'}
_CIPHER = {'1': 'TKIP', '2': 'AES (CCMP)', '3': 'TKIP + AES'}
_PMF = {'0': 'off', '1': 'optional', '2': 'required'}


def parse_radio(radio_id, status, basic, security, advanced, wps):
    bits = base.to_int(_js_first(status, 'band')) or 0
    letters = [name for bit, name in _BAND_BITS if bits & bit]
    width = _select_js(basic, 'chanwid')
    power = _select_js(basic, 'txpower', 'selectedIndex')
    guests = _js_values(status, 'mssid_disable')
    encrypt = _js_first(security, '_encrypt')
    wpa_auth = _js_first(security, '_wpaAuth')
    wps_box = _field(wps, 'disableWPS')
    pin = _field(wps, 'localPin').get('value')
    configured_channel = base.to_int(_js_first(basic, 'defaultChan'))
    band = '5' if bits & (4 | 64) else '2.4'
    sideband = _select_js(basic, 'ctlband')
    return {
        'id': radio_id,
        'band': band,
        'standard': '802.11' + '/'.join(letters) if letters else None,
        'generation': 'Wi-Fi 6' if bits & 128 else 'Wi-Fi 5' if bits & 64 else 'Wi-Fi 4' if bits & 8 else 'Legacy',
        'enabled': _js_first(status, 'wlanDisabled') == '0',
        'state': _js_first(status, 'state_drv'),
        'ssid': _js_first(status, 'ssid_drv'),
        'bssid': base.mac(_js_first(status, 'bssid_drv')),
        'channel': base.to_int(_js_first(status, 'channel_drv')),
        'autoChannel': configured_channel == 0,
        'widthMhz': (20, 40, 80, 160)[width] if width is not None and width < 4 else None,
        # 40 MHz on 2.4 GHz pairs the channel with one 4 channels above or below it
        'sideband': ('upper', 'lower')[sideband] if band == '2.4' and width == 1 and sideband in (0, 1) else None,
        'powerPercent': (100, 70, 50, 35, 15)[power] if power is not None and power < 5 else None,
        'rateLimitMbps': {'tx': base.to_int(_field(basic, 'tx_restrict').get('value')),
                          'rx': base.to_int(_field(basic, 'rx_restrict').get('value'))},
        'security': _SECURITY.get(encrypt, encrypt),
        'auth': 'enterprise (802.1X)' if wpa_auth == '1' else 'personal (PSK)' if wpa_auth == '2' else None,
        'cipher': _CIPHER.get(_js_first(security, '_wpa2uCipher')),
        'pmf': _PMF.get(_js_first(security, '_dotIEEE80211W')),
        'hidden': _choice(advanced, 'hiddenSSID') == 'yes',
        'isolation': _choice(advanced, 'block') == '1',
        'bandSteering': _choice(advanced, 'sta_control') == '1',
        'roaming11k': _choice(advanced, 'dot11kEnabled') == '1',
        'roaming11v': _choice(advanced, 'dot11vEnabled') == '1',
        'beamforming': _choice(advanced, 'txbf') == '1',
        'muMimo': _choice(advanced, 'txbf_mu') == '1',
        'wmm': _choice(advanced, 'WmmEnabled') == '1',
        'multicastToUnicast': _choice(advanced, 'mc2u_disable') == '0',
        'beaconMs': base.to_int(_field(advanced, 'beaconInterval').get('value')),
        'dtim': base.to_int(_field(advanced, 'dtimPeriod').get('value')),
        'wps': {'enabled': bool(wps_box) and 'checked' not in wps_box, 'pin': pin,
                'defaultPin': pin == '12345670', 'lockedOut': _js_first(wps, 'autolockdown_stat') == '1'},
        'guestNetworks': {'used': sum(1 for g in guests if g == '0'), 'slots': len(guests)},
    }


def parse_survey(page, radio_id):
    found = []
    for _, c in base.rows(page):
        if len(c) >= 6 and base.mac(c[1]):
            channel = re.match(r'(\d+)', c[2])
            width = re.search(r'(\d+)\s*MHz', c[2])
            std = re.search(r'\(([^)]*)\)', c[2])
            found.append({'radio': radio_id, 'ssid': c[0], 'bssid': base.mac(c[1]),
                          'channel': int(channel.group(1)) if channel else None,
                          'widthMhz': int(width.group(1)) if width else 20,
                          'standard': std.group(1) if std else None,
                          'security': c[4], 'signal': base.to_int(c[5])})
    return found


def _form_section(page, start):
    """The HTML from a form's opening tag to its </form>, or to end-of-page if the tag is missing."""
    if start < 0:
        return ''
    end = page.find('</form>', start)
    return page[start:end] if end >= 0 else page[start:]


def _table_rows(page, form_name):
    """Cells of rows with a checkbox inside a form, i.e. the console's rule tables."""
    start = page.find('name="%s"' % form_name)
    if start < 0:
        return []
    section = _form_section(page, start)
    return [cells for row, cells in base.rows(section) if re.search(r'type=["\']?checkbox', row, re.I)]


def parse_security(pages):
    p = pages
    acl = []
    for cells in _table_rows(p['acl.asp'], 'acl'):
        if len(cells) >= 5:
            acl.append({'enabled': cells[1].lower() == 'enable', 'side': cells[2], 'source': cells[3],
                        'services': cells[4]})
    forwards = [{'comment': c[1], 'localIp': c[2], 'protocol': c[3], 'localPort': c[4], 'enabled': c[5],
                 'publicPort': c[7] if len(c) > 7 else None}
                for c in _table_rows(p['fw-portfw.asp'], 'formPortFwDel') if len(c) >= 6]
    return {
        'upnp': _choice(p['upnp.asp'], 'daemon') == '1',
        'dmz': {'enabled': _choice(p['fw-dmz.asp'], 'dmzcap') == '1',
                'host': _field(p['fw-dmz.asp'], 'ip').get('value')},
        'portForwarding': {'enabled': _choice(p['fw-portfw.asp'], 'portFwcap') == '1', 'rules': forwards},
        'acl': {'enabled': _choice(p['acl.asp'], 'aclcap') == '1', 'rules': acl},
        'ipv4Filter': {'outgoing': 'deny' if _choice(p['fw-ipportfilter.asp'], 'outAct') == '0' else 'allow',
                       'incoming': 'deny' if _choice(p['fw-ipportfilter.asp'], 'inAct') == '0' else 'allow',
                       'rules': len(_table_rows(p['fw-ipportfilter.asp'], 'formFilterDel'))},
        'ipv6Filter': {'outgoing': 'deny' if _choice(p['fw-ipportfilter-v6.asp'], 'outAct') == '0' else 'allow',
                       'incoming': 'deny' if _choice(p['fw-ipportfilter-v6.asp'], 'inAct') == '0' else 'allow',
                       'rules': len(_table_rows(p['fw-ipportfilter-v6.asp'], 'formFilterDel'))},
        'ipv6': _choice(p['ipv6_enabledisable.asp'], 'ipv6switch') == '1',
        'urlBlocking': _choice(p['url_blocking.asp'], 'urlcap') == '1',
        'domainBlocking': _choice(p['domainblk.asp'], 'domainblkcap') == '1',
        'parentalControl': _choice(p['parental-ctrl.asp'], 'parental_ctrl_on') == '1',
        'syslog': _choice(p['syslog.asp'], 'logcap') == '1',
        'samba': _choice(p['samba.asp'], 'sambaCap') == '1',
    }


SECURITY_PAGES = ('upnp.asp', 'fw-dmz.asp', 'fw-portfw.asp', 'acl.asp', 'fw-ipportfilter.asp',
                  'fw-ipportfilter-v6.asp', 'ipv6_enabledisable.asp', 'url_blocking.asp', 'domainblk.asp',
                  'parental-ctrl.asp', 'syslog.asp', 'samba.asp')


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


def diag_lines(page):
    """Output lines of a running ping/traceroute, from the HTML the result poll returns."""
    page = re.sub(r'<br\s*/?>|</tr>|</p>|</div>', '\n', page, flags=re.I)
    return [line for line in (base.text(part) for part in page.split('\n')) if line]


# --- The driver ---

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RealtekBoa(base.Driver):
    family = 'Realtek Boa GPON ONT (OVT OP2200H and similar)'
    capabilities = frozenset({'devices', 'block', 'limit', 'internet', 'fibre', 'wifi', 'security',
                              'ping', 'traceroute'})

    def __init__(self, host):
        super().__init__(host)
        self.base = 'http://' + host
        self.lock = threading.RLock()  # the router's web server handles one request at a time
        self.opener = urllib.request.build_opener(_NoRedirect)
        self.creds = None  # (username, password), in memory only, set from the panel's login form
        self._radio_info = None  # (read at, {radio id: {bssid, band, ssid, channel}})

    # --- Transport ---

    def _request(self, path, data=None, referer='/'):
        req = urllib.request.Request(self.base + path, data=data)
        req.add_header('Referer', self.base + referer)
        if data is not None:
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            req.add_header('Origin', self.base)
        try:
            with self.opener.open(req, timeout=10) as resp:
                return resp.status, '', resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307):
                return e.code, e.headers.get('Location', ''), ''
            raise RouterError('Router answered HTTP %d for %s' % (e.code, path))
        except http.client.BadStatusLine as e:
            # When logged out, the router sends a bare "You have not logined" page with no headers.
            return 0, '', str(e)
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, ConnectionError) as e:
            raise RouterError('Cannot reach the router at %s (%s)' % (self.host, getattr(e, 'reason', e)))

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
        if message and re.search(r'error|fail|invalid', base.text(message.group(1)), re.I):
            raise RouterError('Router said: ' + base.text(message.group(1)))
        return text

    def _post_result(self, action, referer):
        if action not in RESULT_POSTS:
            raise RouterError('Blocked: %s is not a result page' % action)
        with self.lock:
            _, location, text = self._request(action, b'', referer)
        if self._is_login(location, text):
            raise NotLoggedIn()
        return text

    def _wlan(self, radio_index, page):
        """A per-radio Wi-Fi page. The radio index is stored in the session, so both calls share the lock."""
        with self.lock:
            self.get('/boaform/formWlanRedirect?redirect-url=/%s&wlan_idx=%d' % (page, radio_index))
            return self.get('/' + page)

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

    # --- Reads ---

    def status(self):
        return parse_status(self.get('/status.asp'))

    def _radios_brief(self):
        """{radio id: {bssid, band, ssid, channel}}, re-read every 10 minutes. The BSSIDs let
        the bridge table say which radio a device is on."""
        if not self._radio_info or time.time() - self._radio_info[0] > 600:
            found = {}
            for index, radio_id in enumerate(RADIOS):
                page = self._wlan(index, 'wlstatus.asp')
                bits = base.to_int(_js_first(page, 'band')) or 0
                found[radio_id] = {'bssid': base.mac(_js_first(page, 'bssid_drv')),
                                   'band': '5' if bits & (4 | 64) else '2.4',
                                   'ssid': _js_first(page, 'ssid_drv'),
                                   'channel': base.to_int(_js_first(page, 'channel_drv'))}
            self._radio_info = (time.time(), found)
        return self._radio_info[1]

    def clients(self):
        with self.lock:
            stations = {radio_id: parse_stations(self._wlan(index, 'wlstatbl.asp'))
                        for index, radio_id in enumerate(RADIOS)}
            fdb = parse_fdb(self.get('/fdbtbl.asp'))
            radios = self._radios_brief()
            dhcp = parse_dhcp(self.get('/dhcptbl.asp'))
            arp = parse_arp(self.get('/arptable.asp'))
        bssids = {r['bssid']: radio_id for radio_id, r in radios.items()}
        port_names = {port: bssids[m] for m, (port, local) in fdb.items() if local and m in bssids}
        ports = {m: port_names.get(port) or 'LAN%d' % port for m, (port, local) in fdb.items() if not local}
        return {'dhcp': dhcp, 'arp': arp, 'stations': stations, 'ports': ports, 'radios': radios}

    def blocks(self):
        return parse_mac_rules(self.get('/fw-macfilter.asp'))

    def limits(self):
        return parse_shaping(self.get('/net_qos_traffictl.asp'))

    def internet(self):
        with self.lock:
            return {
                'ipv6': parse_ipv6(self.get('/status_ipv6.asp')),
                'fibre': parse_fibre(self.get('/status_pon.asp'), self.get('/admin/pon-stats.asp')),
                'ports': parse_ports(self.get('/lan_port_status.asp')),
                'interfaces': parse_interfaces(self.get('/stats.asp')),
            }

    def radios(self):
        found = []
        with self.lock:
            for index, radio_id in enumerate(RADIOS):
                pages = [self._wlan(index, p) for p in
                         ('wlstatus.asp', 'wlbasic.asp', 'wlwpa.asp', 'wladvanced.asp', 'wlwps.asp')]
                found.append(parse_radio(radio_id, *pages))
        return found

    def neighbours(self):
        """Networks from the radios' last background scan. Reading the page doesn't start a new scan."""
        with self.lock:
            return [n for index, radio_id in enumerate(RADIOS)
                    for n in parse_survey(self._wlan(index, 'wlsurvey.asp'), radio_id)]

    def security(self):
        with self.lock:
            pages = {name: self.get('/' + name) for name in SECURITY_PAGES}
        return parse_security(pages)

    # --- Writes ---

    def block(self, mac):
        self.post_form('/boaform/admin/formFilter', [
            ('dir', '0'), ('srcmac', mac.replace(':', '')), ('dstmac', ''),
            ('filterMode', 'Deny'), ('addFilterMac', 'Add'),
            ('submit-url', '/admin/fw-macfilter.asp'),
        ], referer='/admin/fw-macfilter.asp')

    def unblock(self, rules):
        if not all(r['field'] for r in rules):
            raise RouterError("Couldn't find this rule's checkbox on the MAC Filtering page")
        self.post_form(
            '/boaform/admin/formFilter',
            [(r['field'], r['value']) for r in rules]
            + [('deleteSelFilterMac', 'Delete Selected'), ('submit-url', '/admin/fw-macfilter.asp')],
            referer='/admin/fw-macfilter.asp')

    def add_limit(self, ip, rate, direction):
        self.post_form('/boaform/admin/formQosTraffictlEdit', shaping_fields(ip, rate, 1 if direction == 'down' else 0),
                       referer='/net_qos_traffictl_edit.asp')

    def remove_limits(self, ids):
        if not all(ids):
            raise RouterError("Couldn't read the Traffic Shaping rule IDs")
        self.post_form('/boaform/admin/formQosTraffictl', [
            ('lst', 'applysetting#id=' + '|'.join(ids)),
            ('submit-url', '/net_qos_traffictl.asp'),
        ], referer='/net_qos_traffictl.asp')

    # --- Diagnostics ---

    def start_diag(self, kind, host):
        if not HOST.match(host or ''):
            raise ValueError('Enter a host name or IPv4 address, such as 1.1.1.1 or google.com')
        if kind == 'ping':
            self.post_form('/boaform/formPing', [
                ('pingAddr', host), ('wanif', '65535'), ('pingAct', 'Start'), ('submit-url', '/ping.asp'),
            ], referer='/ping.asp')
        elif kind == 'traceroute':
            # Fewer tries, a shorter timeout and fewer hops than the console's defaults (3, 5 s, 30),
            # so a trace finishes in well under a minute. The console posts its button as "Stop".
            self.post_form('/boaform/formTracert', [
                ('proto', '0'), ('traceAddr', host), ('trys', '1'), ('timeout', '2'), ('datasize', '56'),
                ('dscp', '0'), ('maxhop', '20'), ('wanif', '65535'), ('tracertAct', 'Start'), ('go', 'Stop'),
                ('submit-url', '/tracert.asp'),
            ], referer='/tracert.asp')
        else:
            raise ValueError('Unknown diagnostic')

    def diag_output(self, kind):
        action, referer = {'ping': ('/boaform/formPingResult', '/ping_result.asp'),
                           'traceroute': ('/boaform/formTracertResult', '/tracert_result.asp')}[kind]
        return diag_lines(self._post_result(action, referer))
