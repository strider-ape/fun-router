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
from html import unescape
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
# on them. Firmware, backup/restore, reboot, WAN/GPON/TR-069, admin password, the Wi-Fi
# name / password / encryption, the MAC filter's default action and every "Delete All"
# button are deliberately unreachable. Wi-Fi writes are limited to turning WPS off and
# changing channel, width and transmit power.
ALLOWED_FORMS = {
    '/boaform/admin/formLogin': {'save'},
    '/boaform/admin/formLogout': {'save'},
    '/boaform/admin/formFilter': {'addFilterMac', 'deleteSelFilterMac'},
    '/boaform/admin/formQosTraffictlEdit': set(),
    '/boaform/admin/formQosTraffictl': set(),
    '/boaform/formPing': set(),
    '/boaform/formTracert': {'go'},
    '/boaform/formDOMAINBLK': {'apply', 'addDomain', 'delDomain'},
    '/boaform/admin/formParentCtrl': {'parentalCtrlSet', 'addfilterMac', 'deleteSelFilterMac'},
    '/boaform/formmacBase': {'addIP', 'delIP'},
    '/boaform/admin/formSysLog': {'apply'},
    '/boaform/formWsc': {'save'},
    '/boaform/admin/formWlanSetup': {'save'},
}
# Posts with an empty body that only read the output of a running diagnostic.
RESULT_POSTS = {'/boaform/formPingResult', '/boaform/formTracertResult'}
BUTTON_FIELDS = {
    'save', 'addFilterMac', 'deleteSelFilterMac', 'setMacDft', 'deleteAllFilterMac', 'go',
    'apply', 'addDomain', 'delDomain', 'delAllDomain', 'parentalCtrlSet', 'addfilterMac',
    'addIP', 'delIP', 'modIP', 'save_log', 'clear_log', 'unlockautolockdown', 'triggerPIN',
    'triggerPBC', 'setPIN', 'suggest_chan_enable',
}
FORBIDDEN_FIELDS = {
    'setMacDft', 'deleteAllFilterMac', 'outAct', 'inAct', 'delAllDomain', 'modIP', 'save_log',
    'clear_log', 'unlockautolockdown', 'triggerPIN', 'triggerPBC', 'setPIN', 'localPin', 'peerPin',
    # Wi-Fi secrets never travel in a request this driver builds
    'pskValue', 'encodepskValue', 'key0', 'encodekey0', 'radiusPass', 'radius2Pass', 'wapiPskValue',
}
# Same rule as the console's own ping/traceroute pages. It also keeps shell metacharacters
# away from the router, which passes the host to its ping and traceroute commands.
# \Z (not $) so a trailing newline can't slip a %0A into the command the router runs.
HOST = re.compile(r'\A(?=.{1,253}\Z)([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9])'
                  r'(\.([a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]))*\Z')
# A domain for the block list: dotted labels only (the console allows 50 characters).
DOMAIN = re.compile(r'\A(?=.{3,50}\Z)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                    r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z')
RULE_NAME = re.compile(r'\A[A-Za-z0-9 ._-]{1,31}\Z')
MAC12 = re.compile(r'\A[0-9a-f]{12}\Z')
MAC_DASH = re.compile(r'\A[0-9a-f]{2}(-[0-9a-f]{2}){5}\Z')
DAYS = ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat')
# Hidden fields the MAC-Based Assignment page sends with every submit (its own checks use them).
_LAN_FIELDS = ('lan_ip', 'lan_mask', 'lan_dhcpRangeStart', 'lan_dhcpRangeEnd', 'lan_dhcpSubnetMask')
POWER_LEVELS = (100, 70, 50, 35, 15)       # txpower select index -> percent
WIDTHS = (20, 40, 80)                      # chanwid select value -> MHz
_5G = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
       149, 153, 157, 161]
# Channels the console offers for regulatory domain 1, read from its own page logic.
# 40 MHz on 2.4 GHz pairs the primary with a channel 4 below ("upper") or above ("lower").
CHANNELS = {
    '2.4': {20: list(range(1, 12)), 40: {'upper': list(range(5, 12)), 'lower': list(range(1, 8))}},
    '5': {20: _5G + [165], 40: _5G, 80: _5G},
}
# Every named field the Wi-Fi basic form has on this firmware. A page with any other
# field is a firmware this driver hasn't been checked against, so it refuses to write.
WLAN_SETUP_FIELDS = {
    'wlanDisabled', 'wlan6gSupport', 'band', 'mode', 'multipleAP', 'ssid', 'chanwid', 'ctlband', 'chan',
    'suggest_chan', 'suggest_chan_enable', 'txpower', 'tx_restrict', 'rx_restrict', 'wl_limitstanum',
    'wl_stanum', 'showMac', 'repeaterEnabled', 'repeaterSSID', 'regdomain_demo', 'submit-url', 'save',
    'basicrates', 'operrates', 'wlan_idx', 'Band2G5GSupport', 'wlanBand2G5GSelect', 'dfs_enable',
    'postSecurityFlag',
}


def _hhmm_ok(h, m):
    return h.isdigit() and m.isdigit() and int(h) <= 23 and int(m) <= 59


def _check_values(action, values):
    """Per-form value rules, on top of the button checks. Raises RouterError."""
    def bad(why):
        raise RouterError('Blocked: %s' % why)

    if action == '/boaform/admin/formQosTraffictl' and not values.get('lst', '').startswith('applysetting#id='):
        bad('unexpected Traffic Shaping request')
    if action == '/boaform/formPing' and (values.get('pingAct') != 'Start' or not HOST.match(values.get('pingAddr', ''))):
        bad('unexpected ping request')
    if action == '/boaform/formTracert' and (values.get('tracertAct') != 'Start'
                                             or not HOST.match(values.get('traceAddr', ''))):
        bad('unexpected traceroute request')
    if action == '/boaform/formDOMAINBLK':
        if values.get('domainblkcap') not in ('0', '1'):
            bad('bad domain-blocking switch')
        if 'addDomain' in values and not DOMAIN.match(values.get('blkDomain', '')):
            bad('not a domain name')
    if action == '/boaform/admin/formParentCtrl':
        if 'parentalCtrlSet' in values and values.get('parental_ctrl_on') not in ('0', '1'):
            bad('bad parental-control switch')
        if 'addfilterMac' in values:
            sh, sm = values.get('starthr', ''), values.get('startmin', '')
            eh, em = values.get('endhr', ''), values.get('endmin', '')
            if not (RULE_NAME.match(values.get('usrname', '')) and MAC12.match(values.get('mac', ''))
                    and _hhmm_ok(sh, sm) and _hhmm_ok(eh, em) and (int(sh), int(sm)) < (int(eh), int(em))
                    and any(values.get(d) == 'on' for d in DAYS)):
                bad('bad schedule')
    if action == '/boaform/formmacBase' and not all(base.IP.match(values.get(k, '')) for k in _LAN_FIELDS):
        bad('the LAN settings must go back exactly as the page holds them')
    if action == '/boaform/formmacBase' and 'addIP' in values:
        ip = values.get('hostIp', '')
        if not (MAC_DASH.match(values.get('hostMac', '')) and base.IP.match(ip)
                and ip.rsplit('.', 1)[0] == values.get('lan_ip', '').rsplit('.', 1)[0]):
            bad('bad IP pin')
    if action == '/boaform/admin/formSysLog':
        # Local logging only: never send the router's log to a server somewhere else.
        if values.get('logcap') not in ('0', '1') or values.get('logMode', '1') != '1' or 'logAddr' in values:
            bad('only local logging can be switched on or off')
    if action == '/boaform/formWsc' and values.get('disableWPS') != 'ON':
        bad('fun-router can only turn WPS off')
    if action == '/boaform/admin/formWlanSetup':
        if 'wlanDisabled' in values or values.get('mode') != '0' or not values.get('ssid'):
            bad('the Wi-Fi radio, mode and name are not changed here')
        if values.get('txpower') not in ('0', '1', '2', '3', '4') or values.get('chanwid') not in ('0', '1', '2'):
            bad('bad Wi-Fi power or width')


def _check_allowed(action, fields):
    if action not in ALLOWED_FORMS:
        raise RouterError('Blocked: %s is not on the allow-list' % action)
    names = {name for name, _ in fields}
    if names & FORBIDDEN_FIELDS:
        raise RouterError('Blocked: forbidden field sent to %s' % action)
    buttons = names & BUTTON_FIELDS
    if buttons - ALLOWED_FORMS[action] or (ALLOWED_FORMS[action] and len(buttons) != 1):
        raise RouterError('Blocked: unexpected button on %s' % action)
    _check_values(action, dict(fields))


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


# --- Controls: rule tables the panel can read, add to and remove from ---
#
# Each *_fields() builder returns a form body exactly as the console's own page sends
# it (same fields, same order, same disabled-field rules). They were checked against the
# router's pages running in a browser, including the postSecurityFlag they compute; the
# vectors live in tests/test_forms.py.

_SELECT = re.compile(r'<select\b([^>]*)>(.*?)</select>', re.S | re.I)
_CELL_HTML = re.compile(r'<t[hd][^>]*>(.*?)(?=<t[hd][\s>]|</t[hd]>|\Z)', re.S | re.I)


def _selects(page):
    """{name: {options: [values], selected: value}} for every <select> (selected = first if none marked)."""
    out = {}
    for attrs, body in _SELECT.findall(page):
        name = re.search(r'name\s*=\s*["\']?([\w\[\]-]+)', attrs)
        if not name:
            continue
        values, selected = [], None
        for opt in re.findall(r'<option\b([^>]*)>', body, re.I):
            v = re.search(r'value\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', opt)
            value = v.group(1).strip('"\'') if v else ''
            values.append(value)
            if selected is None and re.search(r'\bselected\b', opt, re.I):
                selected = value
        out[name.group(1)] = {'options': values, 'selected': selected if selected is not None else (values[0] if values else None)}
    return out


def _form_by_name(page, name):
    for pattern in ('name="%s"', "name='%s'", 'name=%s>', 'name=%s '):
        start = page.find(pattern % name)
        if start >= 0:
            return _form_section(page, page.rfind('<form', 0, start + 1))
    return ''


def _rule_rows(page, form_name, skip=(), last=False):
    """[(cell texts, cell htmls, checkbox name, value)] for table rows with a checkbox or radio.
    skip: control names that aren't rule selectors. last: use the row's last box (the
    parental-control table has day boxes before its Select column)."""
    out = []
    for row, cells in base.rows(_form_by_name(page, form_name)):
        boxes = [b for b in re.findall(r'<input[^>]*type=["\']?(?:checkbox|radio)[^>]*>', row, re.I)
                 if not any(re.search(r'name=["\']?%s\b' % re.escape(n), b) for n in skip)]
        if not boxes:
            continue
        box = boxes[-1] if last else boxes[0]
        name = re.search(r'name=["\']?([\w\[\]]+)', box)
        value = re.search(r'value=["\']?([^"\'\s>]+)', box)
        out.append((cells, _CELL_HTML.findall(row), name.group(1) if name else None, value.group(1) if value else 'on'))
    return out


def parse_domain_blocks(page):
    domains = []
    for cells, _, field, value in _rule_rows(page, 'domainblk', skip=('domainblkcap',)):
        name = cells[-1].strip() if cells else ''
        if DOMAIN.match(name):
            domains.append({'domain': name, 'field': field, 'value': value})
    return {'enabled': _choice(page, 'domainblkcap') == '1', 'domains': domains}


def _day_on(html, text):
    # The table's day cells are only knowable once a rule exists; accept the usual forms.
    return bool(re.search(r'\bchecked\b', html, re.I)) or text.strip().lower() in ('v', 'y', 'yes', 'on', '1', 'x', '*', '✓', '√')


def parse_schedules(page):
    rules = []
    for cells, htmls, field, value in _rule_rows(page, 'formParentCtrlDel', last=True):
        if len(cells) < 11 or not base.mac(cells[1]):
            continue
        days = [d for d, html, text in zip(DAYS, htmls[2:9], cells[2:9]) if _day_on(html, text)]
        rules.append({'name': cells[0], 'mac': base.mac(cells[1]), 'days': days, 'start': cells[9], 'end': cells[10],
                      'field': field, 'value': value})
    return {'enabled': _choice(page, 'parental_ctrl_on') == '1', 'rules': rules}


def parse_pins(page):
    form = _form_by_name(page, 'macBase')
    lan = {i.get('name'): i.get('value', '') for i in base.inputs(form) if i.get('name', '').startswith('lan_')}
    pins = []
    for cells, htmls, field, value in _rule_rows(page, 'macBase', skip=('enable',)):
        if len(cells) < 4:
            continue
        mac, ip = base.mac(cells[2]), cells[3].strip()
        if mac and base.IP.match(ip):
            enabled = 'disable' not in cells[1].lower() and not re.search(r'type=["\']?checkbox(?![^>]*checked)', htmls[1], re.I)
            pins.append({'mac': mac, 'ip': ip, 'enabled': enabled, 'field': field, 'value': value})
    return {'lan': lan, 'pins': pins}


def parse_syslog(page):
    selects = _selects(page)
    entries = []
    for _, c in base.rows(page):
        if len(c) == 4 and c[0] and c[0] != 'Date/Time' and not c[0].endswith(':'):
            entries.append({'time': c[0], 'facility': c[1], 'level': c[2], 'message': c[3]})
    return {'enabled': _choice(page, 'logcap') == '1',
            'level': base.to_int((selects.get('levelLog') or {}).get('selected')), 'entries': entries}


def parse_wlan_setup(page):
    """Current state of the Wi-Fi basic form, as the console's page holds it after its scripts run."""
    form = page[page.find('action=/boaform/admin/formWlanSetup'):]
    form = form[:form.find('</form>')]
    # Fields written by scripts only exist when their condition holds, so look at the plain
    # markup for the field list and treat each script-written field by its own condition.
    markup = re.sub(r'<script\b.*?</script>', '', form, flags=re.S | re.I)
    inputs = [i for i in base.inputs(markup) if i.get('name')]
    selects = _selects(markup)

    def value(name):
        return next((i.get('value', '') for i in inputs if i.get('name') == name), None)

    def checked(name):
        return any('checked' in i for i in inputs if i.get('name') == name)

    names = {i['name'] for i in inputs} | set(selects)
    scripted = set(re.findall(r'name=\\?["\']?(\w+)', ''.join(re.findall(r'<script\b.*?</script>', form, re.S | re.I))))
    # ssidpri ("SSID Priority") is only drawn on the China Mobile build (isCMCCSupport == 1).
    if 'ssidpri' in scripted and _js_first(page, 'isCMCCSupport') != '1':
        scripted.discard('ssidpri')
    names |= scripted
    return {
        'unknown': sorted(names - WLAN_SETUP_FIELDS),
        'band': _select_js(page, 'band'),
        'chanwid': _select_js(page, 'chanwid'),
        'ctlband': _select_js(page, 'ctlband'),
        'txpower': _select_js(page, 'txpower', 'selectedIndex'),
        'chan': base.to_int(_js_first(page, 'defaultChan')),
        'regDomain': base.to_int(_js_first(page, 'regDomain')),
        'wifiTest': base.to_int(_js_first(page, 'WiFiTest')),
        'support8812e': _js_first(page, 'wlan_support_8812e') == '1',
        'ssid': unescape(value('ssid') or ''),
        'mode': (selects.get('mode') or {}).get('selected'),
        'wl_limitstanum': (selects.get('wl_limitstanum') or {}).get('selected'),
        'regdomain_demo': (selects.get('regdomain_demo') or {}).get('selected'),
        'tx_restrict': value('tx_restrict'), 'rx_restrict': value('rx_restrict'), 'wl_stanum': value('wl_stanum') or '',
        'wlanDisabled': checked('wlanDisabled'), 'repeaterEnabled': checked('repeaterEnabled'),
        'wlan6gSupport': checked('wlan6gSupport'),
        'submit_url': value('submit-url'), 'wlan_idx': value('wlan_idx'), 'Band2G5GSupport': value('Band2G5GSupport'),
        'wlanBand2G5GSelect': value('wlanBand2G5GSelect'), 'dfs_enable': value('dfs_enable'),
    }


def wlan_rates(band_value, two_g):
    """basicrates / operrates, as the page's saveChanges() computes them from the band select."""
    band = band_value + 1
    basic = oper = 0
    if band & 1:
        basic |= 0xf
        oper |= 0xf
    if band & 2:
        oper |= 0xff0
        if not band & 1:
            basic = 0xf
    if band & 4:
        oper |= 0xff0
        basic = 0x1f0
    if band & 8:
        if not band & 3:
            oper |= 0xff0
        basic = 0xf if band & 3 else 0x1f0 if band & 4 else 0xf if two_g else 0x1f0
    if band & 64 or band & 128:
        basic = 0xf if two_g else 0x1f0
        oper |= 0xff0
    return basic, oper | basic


def wifi_options(state):
    """Widths and channels this radio can be set to: {width: [channels]} (2.4 GHz 40 MHz merges both sidebands)."""
    band = '2.4' if state.get('Band2G5GSupport') == '1' else '5'
    table = CHANNELS[band]
    return {w: sorted(set(ch['upper'] + ch['lower'])) if isinstance(ch, dict) else list(ch) for w, ch in table.items()}


def wlan_setup_fields(state, width=None, channel=None, power=None):
    """Body of the Wi-Fi basic form changing only width, channel and transmit power.

    The SSID, band, mode and every other field go back exactly as the page holds them.
    Raises RouterError if this page isn't one the driver was checked against.
    """
    if state['unknown'] or state['wlanDisabled'] or state['repeaterEnabled'] or state['wlan6gSupport']:
        raise RouterError('This Wi-Fi page has settings fun-router was not built for; change it on the router\'s own page')
    if state['mode'] != '0' or state['regDomain'] != 1 or state['wifiTest'] or state['band'] is None or not state['ssid']:
        raise RouterError('This radio is in a mode fun-router does not change; use the router\'s own page')
    if (state['chanwid'] not in (0, 1, 2) or state['ctlband'] not in (0, 1) or state['txpower'] not in range(5)
            or not isinstance(state['chan'], int)):
        raise RouterError('Couldn\'t read this radio\'s current settings; use the router\'s own page')
    two_g = state['Band2G5GSupport'] == '1'
    band = '2.4' if two_g else '5'
    cur_w, cur_ch = WIDTHS[state['chanwid']], state['chan']
    new_w = width or cur_w
    new_ch = cur_ch if channel is None else channel
    if power is not None and power not in POWER_LEVELS:
        raise RouterError('Power can be %s%%' % '%, '.join(map(str, POWER_LEVELS)))
    new_power = POWER_LEVELS.index(power) if power is not None else state['txpower']
    if new_w not in CHANNELS[band]:
        raise RouterError('%s GHz can\'t use %d MHz' % (band, new_w))
    sideband = state['ctlband']
    # Channel 0 is "Auto". It can stay as it is (a power-only change), but switching to Auto
    # or changing the width while on Auto is left to the router's own page.
    keep_auto = new_ch == 0 and cur_ch == 0 and new_w == cur_w
    if new_ch == 0 and not keep_auto:
        raise RouterError('Pick a channel: Auto can only be kept as it is here')
    if keep_auto:
        pass
    elif band == '2.4' and new_w == 40:
        # The pair's second channel sits 4 below ("upper") or 4 above ("lower") the primary.
        lists = CHANNELS['2.4'][40]
        if new_ch not in lists['upper'] + lists['lower']:
            raise RouterError('Channel %s is not available at 40 MHz on 2.4 GHz' % new_ch)
        if new_ch not in lists[('upper', 'lower')[sideband]]:
            sideband = 0 if new_ch in lists['upper'] else 1
    elif new_ch not in CHANNELS[band][new_w]:
        raise RouterError('Channel %s is not available at %d MHz on %s GHz' % (new_ch, new_w, band))
    retuned = new_w != cur_w or new_ch != cur_ch or sideband != state['ctlband']
    if retuned:   # the page's channel handler: sideband only for 40 MHz (never above ch 14 on this chip)
        send_sideband = new_w == 40 and not (state['support8812e'] and new_ch > 14)
    else:         # the page's load-time rule
        send_sideband = state['chanwid'] != 0 and new_ch != 0
    basic, oper = wlan_rates(state['band'], two_g)
    fields = [('band', str(state['band'])), ('mode', state['mode']), ('ssid', state['ssid']),
              ('chanwid', str(WIDTHS.index(new_w)))]
    if send_sideband:
        fields.append(('ctlband', str(sideband)))
    fields += [
        ('chan', str(new_ch)), ('txpower', str(new_power)),
        ('tx_restrict', state['tx_restrict']), ('rx_restrict', state['rx_restrict']),
        ('wl_limitstanum', state['wl_limitstanum']), ('wl_stanum', state['wl_stanum']),
        ('regdomain_demo', state['regdomain_demo']), ('submit-url', state['submit_url']),
        ('save', 'Apply Changes'), ('basicrates', str(basic)), ('operrates', str(oper)),
        ('wlan_idx', state['wlan_idx']), ('Band2G5GSupport', state['Band2G5GSupport']),
        ('wlanBand2G5GSelect', state['wlanBand2G5GSelect']), ('dfs_enable', state['dfs_enable']),
    ]
    if any(v is None for _, v in fields):  # a field this firmware's page doesn't have
        raise RouterError('This Wi-Fi page is missing settings fun-router expects; use the router\'s own page')
    return fields


def wps_off_fields(radio_index, version='1'):
    # With "Disable WPS" ticked the page disables the PIN fields and the status radios.
    return [('wlanDisabled', 'OFF'), ('disableWPS', 'ON'), ('wpsUseVersion', version),
            ('submit-url', '/wlwps.asp'), ('save', 'Apply Changes'), ('wlan_idx', str(radio_index))]


def domain_add_fields(enabled, domain):
    return [('domainblkcap', '1' if enabled else '0'), ('blkDomain', domain), ('addDomain', 'Add'),
            ('submit-url', '/domainblk.asp')]


def domain_switch_fields(on):
    return [('domainblkcap', '1' if on else '0'), ('apply', 'Apply Changes'), ('blkDomain', ''),
            ('submit-url', '/domainblk.asp')]


def domain_remove_fields(enabled, rules):
    return ([('domainblkcap', '1' if enabled else '0'), ('blkDomain', '')]
            + [(r['field'], r['value']) for r in rules]
            + [('delDomain', 'Delete Selected'), ('submit-url', '/domainblk.asp')])


def schedule_switch_fields(on):
    return [('parental_ctrl_on', '1' if on else '0'), ('parentalCtrlSet', 'Apply Changes'),
            ('submit-url', '/parental-ctrl.asp')]


def schedule_add_fields(name, mac, days, start, end):
    """start/end: (hour, minute); the console needs start < end on the same day."""
    return ([('usrname', name), ('mac', mac.replace(':', ''))]
            + [(d, 'on') for d in DAYS if d in days]
            + [('starthr', '%02d' % start[0]), ('startmin', '%02d' % start[1]),
               ('endhr', '%02d' % end[0]), ('endmin', '%02d' % end[1]),
               ('addfilterMac', 'Add'), ('submit-url', '/parental-ctrl.asp')])


def schedule_remove_fields(rules):
    return [(r['field'], r['value']) for r in rules] + [('deleteSelFilterMac', 'Delete Selected'),
                                                        ('submit-url', '/parental-ctrl.asp')]




def pin_add_fields(lan, mac, ip):
    return ([(k, lan.get(k, '')) for k in _LAN_FIELDS]
            + [('enable', 'on'), ('hostMac', mac.replace(':', '-')), ('hostIp', ip),
               ('addIP', 'Assign IP'), ('submit-url', '/macIptbl.asp')])


def pin_remove_fields(lan, pin):
    # The row's selector comes after the buttons in the page; its exact shape is confirmed live.
    return ([(k, lan.get(k, '')) for k in _LAN_FIELDS]
            + ([('enable', 'on')] if pin['enabled'] else [])
            + [('hostMac', pin['mac'].replace(':', '-')), ('hostIp', pin['ip']), ('delIP', 'Delete Assigned IP'),
               ('submit-url', '/macIptbl.asp'), (pin['field'], pin['value'])])


def syslog_fields(on, level=6):
    if not on:  # with logging off the page disables the level and server fields
        return [('logcap', '0'), ('apply', 'Apply Changes'), ('submit-url', '/admin/syslog.asp')]
    return [('logcap', '1'), ('levelLog', str(level)), ('levelDisplay', str(level)), ('logMode', '1'),
            ('apply', 'Apply Changes'), ('submit-url', '/admin/syslog.asp')]


# --- The driver ---

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RealtekBoa(base.Driver):
    family = 'Realtek Boa GPON ONT (OVT OP2200H and similar)'
    capabilities = frozenset({'devices', 'block', 'limit', 'internet', 'fibre', 'wifi', 'security',
                              'ping', 'traceroute', 'usage', 'domains', 'schedules', 'pins', 'syslog',
                              'wps', 'wifi-tune'})

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

    def logout(self):
        """End this computer's console session. Forgets the saved credentials first, so the
        panel doesn't quietly log straight back in."""
        with self.lock:
            self.creds = None
            self.post_form('/boaform/admin/formLogout', [
                ('save', 'Logout'), ('submit-url', '/admin/logout.asp'),
            ], referer='/admin/logout.asp', check_session=False)

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

    def counters(self):
        """(bytes down, bytes up) through the fibre since boot. One small page, cheap to poll."""
        stats = base.pairs(self.get('/admin/pon-stats.asp'))
        down, up = base.to_int(stats.get('Bytes Received')), base.to_int(stats.get('Bytes Sent'))
        return (down, up) if down is not None and up is not None else None

    def optics(self):
        """(rx dBm, tx dBm) of the fibre transceiver."""
        s = base.pairs(self.get('/status_pon.asp'))
        return base.number(s.get('Rx Power')), base.number(s.get('Tx Power'))

    def radios(self):
        found = []
        with self.lock:
            for index, radio_id in enumerate(RADIOS):
                pages = [self._wlan(index, p) for p in
                         ('wlstatus.asp', 'wlbasic.asp', 'wlwpa.asp', 'wladvanced.asp', 'wlwps.asp')]
                radio = parse_radio(radio_id, *pages)
                setup = parse_wlan_setup(pages[1])
                try:
                    wlan_setup_fields(setup)  # can this page be written safely at all?
                    radio['tune'] = {'supported': True, 'options': wifi_options(setup)}
                except RouterError as e:
                    radio['tune'] = {'supported': False, 'why': str(e), 'options': {}}
                found.append(radio)
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

    # --- Controls ---

    def domain_blocks(self):
        return parse_domain_blocks(self.get('/domainblk.asp'))

    def add_domain(self, domain):
        with self.lock:
            cur = self.domain_blocks()
            if not any(d['domain'].lower() == domain.lower() for d in cur['domains']):
                self.post_form('/boaform/formDOMAINBLK', domain_add_fields(cur['enabled'], domain), referer='/domainblk.asp')
            if not cur['enabled']:
                self.post_form('/boaform/formDOMAINBLK', domain_switch_fields(True), referer='/domainblk.asp')

    def remove_domains(self, domains):
        with self.lock:
            cur = self.domain_blocks()
            wanted = {d.lower() for d in domains}
            rules = [d for d in cur['domains'] if d['domain'].lower() in wanted]
            if not rules:
                return
            if not all(r['field'] for r in rules):
                raise RouterError("Couldn't find the domain's checkbox on the Domain Blocking page")
            self.post_form('/boaform/formDOMAINBLK', domain_remove_fields(cur['enabled'], rules), referer='/domainblk.asp')

    def set_domain_blocking(self, on):
        self.post_form('/boaform/formDOMAINBLK', domain_switch_fields(on), referer='/domainblk.asp')

    def schedules(self):
        return parse_schedules(self.get('/parental-ctrl.asp'))

    def add_schedule(self, name, mac, days, start, end):
        with self.lock:
            cur = self.schedules()
            self.post_form('/boaform/admin/formParentCtrl', schedule_add_fields(name, mac, days, start, end),
                           referer='/parental-ctrl.asp')
            if not cur['enabled']:
                self.post_form('/boaform/admin/formParentCtrl', schedule_switch_fields(True), referer='/parental-ctrl.asp')

    def remove_schedules(self, rules):
        if not all(r['field'] for r in rules):
            raise RouterError("Couldn't find the schedule's checkbox on the Parental Control page")
        self.post_form('/boaform/admin/formParentCtrl', schedule_remove_fields(rules), referer='/parental-ctrl.asp')

    def set_schedules(self, on):
        self.post_form('/boaform/admin/formParentCtrl', schedule_switch_fields(on), referer='/parental-ctrl.asp')

    def pins(self):
        return parse_pins(self.get('/macIptbl.asp'))

    def add_pin(self, mac, ip):
        with self.lock:
            cur = self.pins()
            self.post_form('/boaform/formmacBase', pin_add_fields(cur['lan'], mac, ip), referer='/macIptbl.asp')

    def remove_pin(self, mac):
        with self.lock:
            cur = self.pins()
            pin = next((p for p in cur['pins'] if p['mac'] == mac), None)
            if not pin:
                return
            if not pin['field']:
                raise RouterError("Couldn't find the pin's selector on the MAC-Based Assignment page")
            self.post_form('/boaform/formmacBase', pin_remove_fields(cur['lan'], pin), referer='/macIptbl.asp')

    def syslog(self):
        return parse_syslog(self.get('/syslog.asp'))

    def set_syslog(self, on):
        self.post_form('/boaform/admin/formSysLog', syslog_fields(on), referer='/admin/syslog.asp')

    @staticmethod
    def _radio_index(radio_id):
        if radio_id not in RADIOS:
            raise ValueError('Unknown radio')
        return RADIOS.index(radio_id)

    def disable_wps(self, radio_id):
        index = self._radio_index(radio_id)
        with self.lock:
            page = self._wlan(index, 'wlwps.asp')
            version = _js_first(page, 'wpsUseVersion') or '1'
            self.post_form('/boaform/formWsc', wps_off_fields(index, version), referer='/wlwps.asp')

    def wifi_state(self, radio_id):
        """How one radio is configured: {ssid, width (MHz), channel (0 = Auto), power (%)}."""
        s = parse_wlan_setup(self._wlan(self._radio_index(radio_id), 'wlbasic.asp'))
        return {'ssid': s['ssid'], 'width': WIDTHS[s['chanwid']] if s['chanwid'] in (0, 1, 2) else None,
                'channel': s['chan'], 'power': POWER_LEVELS[s['txpower']] if s['txpower'] in range(5) else None}

    def tune_wifi(self, radio_id, width=None, channel=None, power=None):
        """Change width / channel / power. The radio restarts, so clients drop for a few
        seconds; if this computer is on that radio the request itself may not get an answer."""
        index = self._radio_index(radio_id)
        with self.lock:
            state = parse_wlan_setup(self._wlan(index, 'wlbasic.asp'))
            fields = wlan_setup_fields(state, width=width, channel=channel, power=power)
            try:
                self.post_form('/boaform/admin/formWlanSetup', fields, referer='/admin/wlbasic.asp')
            except RouterError as e:
                if 'Cannot reach' not in str(e):
                    raise
                # The radio restart can cut this computer off mid-request; the caller verifies.
            return state['ssid']

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
