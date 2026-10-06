"""Form bodies and checksums, checked against the router's own pages; plus the allow-list
and the channel suggestion.

Each expected postSecurityFlag below was produced by the console's own JavaScript
(its page handlers + postTableEncrypt) running in a browser on a saved copy of the page,
with placeholder SSIDs and MACs. If a builder drifts from what the page sends, the
checksum changes and the router would reject the request.

Run:  py -m unittest discover tests      (python3 on macOS/Linux)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from routers import realtek_boa as rb  # noqa: E402
from routers.base import RouterError  # noqa: E402

LAN = {'lan_ip': '192.168.1.1', 'lan_mask': '255.255.255.0', 'lan_dhcpRangeStart': '192.168.1.2',
       'lan_dhcpRangeEnd': '192.168.1.254', 'lan_dhcpSubnetMask': '255.255.255.0'}


def flag(fields):
    return int(rb.encode_form(fields).rsplit('=', 1)[1])


def wlan_state(**over):
    state = {
        'unknown': [], 'band': 10, 'chanwid': 1, 'ctlband': 0, 'txpower': 0, 'chan': 11, 'regDomain': 1,
        'wifiTest': 0, 'support8812e': True, 'ssid': 'Placeholder', 'mode': '0', 'wl_limitstanum': '0',
        'regdomain_demo': '1', 'tx_restrict': '0', 'rx_restrict': '0', 'wl_stanum': '', 'wlanDisabled': False,
        'repeaterEnabled': False, 'wlan6gSupport': False, 'submit_url': '/admin/wlbasic.asp', 'wlan_idx': '1',
        'Band2G5GSupport': '1', 'wlanBand2G5GSelect': '0', 'dfs_enable': '1',
    }
    state.update(over)
    return state


class Checksum(unittest.TestCase):
    def test_original_vectors(self):
        # postTableEncrypt() from the router's common.js, run on these exact fields.
        self.assertEqual(flag([('dir', '0'), ('srcmac', 'aabbccddeeff'), ('dstmac', ''), ('filterMode', 'Deny'),
                               ('addFilterMac', 'Add'), ('submit-url', '/admin/fw-macfilter.asp')]), 10381)
        self.assertEqual(flag([('a', "x y!'()~*+/=é"), ('lst', 'applysetting#id=1|2'),
                               ('submit-url', '/net_qos_traffictl.asp')]), 2949)


class Builders(unittest.TestCase):
    def test_wps_off(self):
        f = rb.wps_off_fields(1)
        self.assertEqual(f, [('wlanDisabled', 'OFF'), ('disableWPS', 'ON'), ('wpsUseVersion', '1'),
                             ('submit-url', '/wlwps.asp'), ('save', 'Apply Changes'), ('wlan_idx', '1')])
        self.assertEqual(flag(f), 5200)

    def test_domain(self):
        self.assertEqual(flag(rb.domain_add_fields(False, 'example.com')), 63242)
        self.assertEqual(flag(rb.domain_switch_fields(True)), 48917)

    def test_schedule(self):
        f = rb.schedule_add_fields('Bedtime', 'aa:bb:cc:dd:ee:ff', ['Mon', 'Tue'], (23, 0), (23, 59))
        self.assertEqual(flag(f), 13666)
        self.assertEqual(flag(rb.schedule_switch_fields(True)), 47823)

    def test_pin(self):
        self.assertEqual(flag(rb.pin_add_fields(LAN, 'aa:bb:cc:dd:ee:ff', '192.168.1.6')), 51786)

    def test_syslog(self):
        self.assertEqual(flag(rb.syslog_fields(True, 6)), 48680)

    def test_wifi_24_to_40_lower(self):
        # SSID with a space, & and brackets; 40 MHz on channel 3 needs the "lower" sideband.
        f = rb.wlan_setup_fields(wlan_state(ssid='My Home & Co (2G)'), width=40, channel=3, power=50)
        self.assertIn(('ctlband', '1'), f)
        self.assertEqual(flag(f), 62539)

    def test_wifi_5_channel_change_drops_sideband(self):
        state = wlan_state(ssid='Placeholder_5G', band=75, chanwid=2, chan=44, wlan_idx='0', Band2G5GSupport='2')
        f = rb.wlan_setup_fields(state, channel=149)
        self.assertNotIn('ctlband', dict(f))
        self.assertEqual(flag(f), 46306)

    def test_wifi_auto_channel_kept_for_power_change(self):
        # The 2.4 GHz page with defaultChan=0 ("Auto") and power set to 70%: the page disables
        # the sideband select at load, so only chan=0 goes back.
        f = rb.wlan_setup_fields(wlan_state(chan=0), power=70)
        self.assertNotIn('ctlband', dict(f))
        self.assertEqual(flag(f), 14315)

    def test_rates(self):
        self.assertEqual(rb.wlan_rates(10, True), (15, 4095))     # 2.4 GHz b/g/n
        self.assertEqual(rb.wlan_rates(75, False), (496, 4080))   # 5 GHz a/n/ac

    def test_wifi_refuses_unknown_pages_and_bad_channels(self):
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(unknown=['ssidpri']), power=70)
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(repeaterEnabled=True), power=70)
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(), width=20, channel=13)   # not offered in this region
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(), width=80)               # 2.4 GHz has no 80 MHz
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(chan=0), width=20)         # width change while on Auto
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(), channel=0)              # switching to Auto
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(), power=60)               # not a level the page offers
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(chanwid=None), power=70)   # current settings unreadable
        with self.assertRaises(RouterError):
            rb.wlan_setup_fields(wlan_state(tx_restrict=None), power=70)  # a field this page lacks


class AllowList(unittest.TestCase):
    def blocked(self, action, fields):
        with self.assertRaises(RouterError):
            rb._check_allowed(action, fields)

    def test_never_reachable(self):
        self.blocked('/boaform/admin/formWlEncrypt', [('save', 'Apply Changes')])        # Wi-Fi password form
        self.blocked('/boaform/formWsc', [('wlanDisabled', 'OFF'), ('save', 'Apply Changes')])  # WPS on
        self.blocked('/boaform/formWsc', [('disableWPS', 'ON'), ('triggerPBC', 'Start PBC')])
        self.blocked('/boaform/formDOMAINBLK', [('domainblkcap', '1'), ('delAllDomain', 'Delete All')])
        self.blocked('/boaform/admin/formParentCtrl', [('deleteAllFilterMac', 'Delete All')])
        self.blocked('/boaform/admin/formSysLog', [('logcap', '1'), ('logMode', '2'), ('logAddr', '203.0.113.9'),
                                                   ('apply', 'Apply Changes')])
        self.blocked('/boaform/admin/formWlanSetup', [('wlanDisabled', 'ON'), ('mode', '0'), ('ssid', 'x'),
                                                      ('txpower', '0'), ('chanwid', '0'), ('save', 'Apply Changes')])
        self.blocked('/boaform/admin/formWlanSetup', [('mode', '1'), ('ssid', 'x'), ('txpower', '0'),
                                                      ('chanwid', '0'), ('save', 'Apply Changes')])

    def test_value_rules(self):
        self.blocked('/boaform/formDOMAINBLK', rb.domain_add_fields(True, 'evil.com;reboot'))
        self.blocked('/boaform/formPing', [('pingAddr', '1.1.1.1\n'), ('wanif', '65535'), ('pingAct', 'Start')])
        self.blocked('/boaform/admin/formParentCtrl',
                     rb.schedule_add_fields('Bed', 'aa:bb:cc:dd:ee:ff', ['Mon'], (23, 0), (7, 0)))   # crosses midnight
        self.blocked('/boaform/formmacBase', rb.pin_add_fields(LAN, 'aa:bb:cc:dd:ee:ff', '10.0.0.5'))    # off-LAN IP
        self.blocked('/boaform/formmacBase', rb.pin_add_fields(LAN, 'aa:bb:cc:dd:ee:ff', '192.168.1.6\n'))
        self.blocked('/boaform/formmacBase', rb.pin_add_fields({'lan_ip': '192.168.1.1'}, 'aa:bb:cc:dd:ee:ff',
                                                               '192.168.1.6'))   # LAN fields not sent back as read

    def test_allowed(self):
        for action, fields in (
            ('/boaform/formWsc', rb.wps_off_fields(0)),
            ('/boaform/formDOMAINBLK', rb.domain_add_fields(True, 'example.com')),
            ('/boaform/admin/formParentCtrl', rb.schedule_add_fields('Bed', 'aa:bb:cc:dd:ee:ff', ['Mon'], (0, 0), (7, 0))),
            ('/boaform/admin/formSysLog', rb.syslog_fields(True)),
            ('/boaform/admin/formWlanSetup', rb.wlan_setup_fields(wlan_state(), width=20, channel=1)),
            ('/boaform/formmacBase', rb.pin_add_fields(LAN, 'aa:bb:cc:dd:ee:ff', '192.168.1.6')),
        ):
            rb._check_allowed(action, fields)


class ChannelAdvice(unittest.TestCase):
    """The one-tap channel suggestion compares real frequency spans, not channel numbers."""

    def radio(self, **over):
        r = {'band': '5', 'channel': 44, 'widthMhz': 80, 'enabled': True, 'bssid': 'aa:aa:aa:aa:aa:01',
             'tune': {'options': rb.wifi_options({'Band2G5GSupport': '2'})}}
        r.update(over)
        return r

    def test_spans(self):
        self.assertEqual(server.channel_span('2.4', 1, 20), (2402, 2422))
        self.assertEqual(server.channel_span('2.4', 11, 40, 'upper'), (2432, 2472))   # 11 bonded with 7
        self.assertEqual(server.channel_span('5', 44, 80), (5170, 5250))              # the 36-48 block
        self.assertEqual(server.channel_span('5', 157, 40), (5775, 5815))             # 157 + 161

    def test_next_80mhz_block_does_not_count(self):
        # A strong network on 52 at 80 MHz sits in the 52-64 block, right next to 36-48.
        near = [{'channel': 52, 'widthMhz': 80, 'signal': 90, 'bssid': 'bb:bb:bb:bb:bb:01'}]
        self.assertIsNone(server.recommend_channel(self.radio(), near))

    def test_moves_off_a_shared_block(self):
        near = [{'channel': 40, 'widthMhz': 80, 'signal': 80, 'bssid': 'bb:bb:bb:bb:bb:02'}]
        rec = server.recommend_channel(self.radio(), near)
        self.assertEqual((rec['channel'], rec['width']), (149, 80))

    def test_24_picks_the_quiet_clean_channel(self):
        r = self.radio(band='2.4', channel=11, widthMhz=40, sideband='upper',
                       tune={'options': rb.wifi_options({'Band2G5GSupport': '1'})})
        near = [{'channel': 11, 'widthMhz': 20, 'signal': 70, 'bssid': 'cc:cc:cc:cc:cc:01'},
                {'channel': 6, 'widthMhz': 20, 'signal': 60, 'bssid': 'cc:cc:cc:cc:cc:02'}]
        rec = server.recommend_channel(r, near)
        self.assertEqual((rec['channel'], rec['width']), (1, 20))


if __name__ == '__main__':
    unittest.main()
