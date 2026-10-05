/* What each setting or reading means, in plain technical terms.
 *
 * Every entry has:
 *   what    - what the thing is and how the router implements it
 *   change  - what it affects, or what happens to traffic/devices when it changes
 *   example - a concrete case, using the live values in `c` where they help
 * Each field is a string, or a function of a small context object the page passes in.
 * Entries marked `effect: READING` are read-only facts, not settings this app changes.
 * Shown only when the user opens a "How it works" toggle (hidden by default).
 */
(() => {
  const v = (x) => String(x ?? '?').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const READING = 'What it affects';

  window.EXPLAIN = {
    /* ---------- Devices ---------- */
    'device.location': {
      title: 'Where a device shows up',
      what: 'Devices on the router\'s own radios appear in each radio\'s station table. For everything else the panel reads the bridge forwarding database (FDB): the table mapping each source MAC address to the switch port its frames last arrived on.',
      change: 'Anything behind a second access point or switch shows up on the LAN port that box plugs into, because the router only sees its frames after they cross that cable. FDB entries expire after about 5 minutes of silence, so an idle device can show no location.',
      example: 'A phone on an upstairs access point sends frames through that AP into LAN1. The FDB lists the phone\'s MAC on port 1, so the panel shows it on LAN1 even though it is really on Wi-Fi upstairs.',
      effect: READING,
    },
    'device.online': {
      title: 'Online and offline',
      what: 'A device counts as online if it is associated to a radio right now, or if the router has it in its ARP cache (the IPv4-to-MAC table it uses to deliver packets on the LAN).',
      change: 'ARP entries linger a few minutes after a device leaves, so one that just walked out can still read online. An offline device with a lease is one the router gave an address to but has not heard from lately.',
      example: 'A lease of 86400 s means the address is reserved for 24 hours. The device tries to renew at half that (12 h) while it stays connected.',
      effect: READING,
    },
    'device.signal': {
      title: 'Signal, link rate and live speed',
      what: 'RSSI is the strength of the device\'s frames as the router hears them, in dBm (decibels relative to 1 mW). The link rate is the PHY rate the radio last used to reach it, picked from the modulation the signal can carry (the MCS index). Live speed is the change in the station\'s byte counters between two polls.',
      change: 'Every 10 dB lower is 10x less power. Roughly: -50 dBm or better is excellent, -67 dBm is the usual floor for video calls, -75 dBm is shaky, and below -85 dBm the link drops to its slowest rates, which also costs everyone airtime because a slow frame holds the channel longer.',
      example: (c) => c && c.rssi != null
        ? `This device is at <code>${v(c.rssi)} dBm</code>, link rate <code>${v(c.rate)} Mb/s</code>. ${c.rssi < -80 ? 'That is weak enough that frames to it go out at a very low rate. If it sits near another AP with the same SSID it is probably "sticky" — clients only roam when their own threshold trips, so toggling its Wi-Fi off and on makes it pick the closer AP.' : c.rssi < -67 ? 'Usable, but video calls may stutter.' : 'A healthy signal.'}`
        : 'Live speed only shows for devices on the router\'s own radios. Traffic through a LAN port is not counted per device.',
      effect: READING,
    },
    'device.privateMac': {
      title: 'Private (randomised) MAC addresses',
      what: 'Phones make up a random MAC per network instead of using their real one. You can spot it: the second-lowest bit of the first byte (the "locally administered" bit) is set, so the first byte ends in 2, 6, A or E.',
      change: 'Blocks and names are keyed to the MAC. If the phone rotates its random MAC (Android can per connection, iOS on a schedule or after "forget network"), the router sees a brand-new device — the old block stops matching and the lease changes.',
      example: '<code>fa:84:70:…</code> starts with 0xFA = 1111 1010; bit 1 is set, so it is random. To make a block stick, set that phone to use its device MAC for this network.',
      effect: READING,
    },
    'device.block': {
      title: 'Block (MAC filter)',
      what: 'Adds an "outgoing, source MAC, deny" rule to the router\'s MAC filter, which drops that MAC\'s frames on the way out to the internet. Deleting the rule restores access.',
      change: 'The device stays on the Wi-Fi and keeps its lease and LAN access (printing, casting to a TV on the same network). Only internet-bound traffic is dropped. This firmware labels the page "MAC Filtering for bridge mode", so the first live test checks the rule also catches routed (PPPoE) traffic.',
      example: 'Blocking <code>aa:bb:cc:dd:ee:ff</code> posts <code>dir=0, srcmac=aabbccddeeff, filterMode=Deny</code> — the same request the console\'s own "Add" button sends.',
    },
    'device.limit': {
      title: 'Speed limit (traffic shaping)',
      what: 'Adds IP QoS traffic-shaping rules on the WAN interface. A download limit matches packets whose destination IP is the device; an upload limit matches its source IP. Packets over the rate are queued, then dropped.',
      change: 'TCP backs off when it sees the delay and drops, so the device settles near the limit. Bursty UDP (games, calls) just loses packets. The rule follows the IP, not the MAC — if DHCP later gives the device a different IP, the limit stays on the old one.',
      example: 'A 2 Mb/s download cap on 192.168.1.6 pushes a 4K stream (15-25 Mb/s) down to its lowest quality while browsing still works.',
    },

    /* ---------- Internet ---------- */
    'wan.session': {
      title: 'The internet connection (PPPoE over fibre)',
      what: (c) => `The router logs in to the ISP with PPPoE over VLAN ${v(c && c.vlan)} on the fibre link. The session (interface <code>${v(c && c.iface)}</code>) gets IPv4 via IPCP and IPv6 via DHCPv6 prefix delegation. PPPoE\'s 8-byte header drops the usable MTU to 1492 bytes from the normal 1500.`,
      change: 'When the session drops (fibre fault, ISP maintenance, reboot) the router redials. A new session can bring a new IPv4 address and IPv6 prefix, and every TCP connection through the router breaks.',
      example: (c) => `Up for <code>${v(c && c.up)}</code>. The far end of the PPP link is the ISP\'s broadband gateway at <code>${v(c && c.gateway)}</code> — also the first hop in a traceroute.`,
      effect: READING,
    },
    'wan.address': {
      title: 'Your WAN IPv4 address',
      what: (c) => c && c.kind !== 'public'
        ? `The ISP gave you <code>${v(c.ip)}</code>, which is ${c.kind === 'cgnat' ? 'inside 100.64.0.0/10 — the range reserved for carrier-grade NAT (RFC 6598)' : 'a private address (RFC 1918)'}. The ISP then translates it to a public address shared with other customers. This is carrier-grade NAT (CGNAT).`
        : `The ISP gave your router the public address <code>${v(c && c.ip)}</code> directly.`,
      change: (c) => c && c.kind !== 'public'
        ? 'Outbound works normally. Unsolicited inbound IPv4 is dropped at the ISP before it reaches you, so port forwarding, DMZ and UPnP on this router do nothing from the internet over IPv4. Reaching a device from outside needs IPv6, or a tunnel/relay (a VPN with port forwarding, a reverse proxy), or asking the ISP for a public IP.'
        : 'Port forwarding works: the router rewrites the inbound packet to the chosen LAN device.',
      example: (c) => c && c.kind !== 'public'
        ? `A game server on 192.168.1.13 is unreachable at your public IPv4 even with a forward, because the packet never reaches <code>${v(c.ip)}</code>. It is reachable on that PC\'s public IPv6 address if the IPv6 firewall allows the port.`
        : 'Forward TCP 25565 to 192.168.1.13 and a Minecraft server there is reachable at your public IP.',
      effect: READING,
    },
    'wan.ipv6': {
      title: 'IPv6',
      what: (c) => `The ISP delegated the prefix <code>${v(c && c.prefix)}</code>. The router advertises it on the LAN (router advertisements), and each device builds its own addresses from it (SLAAC), usually adding rotating "temporary" ones for privacy.`,
      change: 'Every device gets globally routable addresses, so no NAT is in the path. Whether they are reachable from outside is decided only by the router\'s IPv6 firewall, which drops unsolicited inbound by default here. If the prefix changes after a reconnect, devices pick up new addresses within minutes.',
      example: (c) => `A device might hold <code>${v(c && c.example)}</code>. IPv6-capable sites see that, not your IPv4 NAT address.`,
      effect: READING,
    },
    'wan.dns': {
      title: 'DNS servers',
      what: 'The resolvers the router learned from the ISP. LAN devices usually ask the router at the gateway address, which forwards to these and answers reverse lookups for local DHCP host names itself.',
      change: 'Slow or failing resolvers make every new site feel slow even when bandwidth is fine. A device can bypass the router with DNS-over-HTTPS (built into browsers) or its own resolver setting.',
      example: (c) => `Your router forwards to <code>${v(c && c.dns)}</code>.`,
      effect: READING,
    },
    'fibre.rx': {
      title: 'Fibre receive power',
      what: 'The optical power reaching the router\'s receiver from the ISP, on the 1490 nm downstream wavelength. dBm is decibels relative to 1 mW, so -27 dBm is about 2 microwatts. Splitters, splices, connectors and fibre length each subtract a few dB.',
      change: 'GPON optics have a class: class B+ receivers are rated down to -27 dBm, class C+ to -30 dBm, and both saturate above about -8 dBm. Near the limit, bit errors climb; forward error correction hides them until it can\'t, then the link drops (the ONT light goes red).',
      example: (c) => c && c.rx != null
        ? `Yours reads <code>${v(c.rx.toFixed(2))} dBm</code>. ${c.rx < -27 ? 'That is past the class B+ limit — expect drops. A technician should clean and check the connectors.' : c.rx < -25 ? 'Close to the class B+ limit. Watch the FEC counter: if it starts climbing, a dirty connector or a tight bend in the patch cord is the usual cause, and fixing it often gains 1-3 dB.' : 'Comfortable margin.'}`
        : 'This router does not report optical levels.',
      effect: READING,
    },
    'fibre.tx': {
      title: 'Fibre transmit power',
      what: 'The router\'s own laser output on the 1310 nm upstream wavelength. In GPON the router only transmits in timeslots the ISP\'s terminal grants it, which lets dozens of homes share one fibre through a passive splitter.',
      change: 'Class B+ lasers are rated +0.5 to +5 dBm. Low output together with a rising bias current points to an ageing laser.',
      example: (c) => `Yours is <code>${v(c && c.tx != null ? c.tx.toFixed(2) : '?')} dBm</code> at a bias current of <code>${v(c && c.bias)} mA</code>.`,
      effect: READING,
    },
    'fibre.onu': {
      title: 'ONU state',
      what: 'The GPON activation state (ITU-T G.984.3): O1 initial, O2 standby, O3 serial-number, O4 ranging, O5 operating, O6 intermittent signal loss, O7 emergency stop.',
      change: 'Only O5 passes traffic. Stuck at O2/O3 means the router sees light but is not registered — usually a provisioning issue at the ISP. O6 means the signal keeps dropping; O7 means the ISP disabled this unit.',
      example: (c) => `Yours is <code>${v(c && c.state)}</code>${c && c.state === 'O5' ? ' — registered and passing traffic.' : '.'}`,
      effect: READING,
    },
    'fibre.errors': {
      title: 'FEC and HEC error counters',
      what: 'FEC (forward error correction) adds redundancy so the receiver repairs corrupted bytes without a resend; the FEC counter is how many it fixed. HEC errors are frame headers too damaged to read, so that frame was lost.',
      change: 'Zero or slowly rising is normal. A fast-rising FEC count means the optical level is marginal. Any HEC errors mean real frame loss.',
      example: (c) => `Since the last reboot: FEC <code>${v(c && c.fec)}</code>, HEC <code>${v(c && c.hec)}</code>.`,
      effect: READING,
    },
    'usage': {
      title: 'Data used since the last reboot',
      what: 'Byte counters on the fibre interface since the router last restarted. They cover every device, IPv4 and IPv6, including PPPoE and framing overhead.',
      change: 'They reset on reboot. Your ISP bills on its own counters, which can differ by a few percent because of that overhead.',
      example: (c) => c && c.perDay ? `Averaging about <code>${v(c.perDay)}</code> down per day over <code>${v(c.days)}</code> days.` : '',
      effect: READING,
    },
    'lan.ports': {
      title: 'LAN port link speed',
      what: 'The speed and duplex each Ethernet port auto-negotiated with whatever is plugged in. Both ends must support a speed for it to be used.',
      change: '100 Mb Full (Fast Ethernet) caps everything behind that port at about 94 Mb/s of real throughput, shared by all devices behind it. 1000 Mb needs gigabit ports at both ends and a cable with all 4 pairs intact (Cat5e+). A cable with a broken pair often drops to 100 Mb.',
      example: (c) => c && c.slow ? `<code>${v(c.slow)}</code> came up at 100 Mb. If that is the upstairs access point (a TL-WR850N has only 10/100 ports), everything upstairs shares at most ~94 Mb/s.` : 'All connected ports negotiated their full speed.',
      effect: READING,
    },
    'iface.errors': {
      title: 'Interface error counters',
      what: 'Per-interface packet counters since boot. TX errors on a radio are frames that failed even after the radio\'s own retries (interference, collisions, far-away clients). RX errors on Ethernet usually mean a bad cable or port.',
      change: 'A few thousand Wi-Fi TX errors against millions of packets is normal background. A count climbing fast points to a crowded channel or a very weak client.',
      example: (c) => c && c.worst ? `<code>${v(c.worst.name)}</code>: ${v(c.worst.txErrors)} TX errors in ${v(c.worst.txPackets)} packets (${v(c.worst.pct)}%).` : '',
      effect: READING,
    },

    /* ---------- Wi-Fi ---------- */
    'wifi.channel': {
      title: 'Channel',
      what: (c) => c && c.band === '2.4'
        ? '2.4 GHz channels sit 5 MHz apart but each signal is ~20 MHz wide, so neighbours overlap. Only 1, 6 and 11 don\'t overlap each other.'
        : '5 GHz channels are 20 MHz apart and don\'t overlap. 36-48 are always usable. 52-144 are DFS: the router must listen 60 s for radar first and leave if it hears any. 149-165 depend on the country.',
      change: 'Changing channel restarts the radio, so clients drop for a few seconds and reconnect. Two networks on the same channel take turns (they share airtime). Two on partly overlapping 2.4 GHz channels corrupt each other\'s frames, which is worse than sharing.',
      example: (c) => c ? `This radio is on channel <code>${v(c.channel)}</code> at ${v(c.width)} MHz.${c.overlaps ? ` It overlaps ${v(c.overlaps)} nearby network${c.overlaps === 1 ? '' : 's'} from the last scan.` : ''}` : '',
    },
    'wifi.width': {
      title: 'Channel width',
      what: 'How many 20 MHz channels the radio bonds into one. Double the width is roughly double the peak rate, at the cost of more spectrum and more interference picked up.',
      change: (c) => c && c.band === '2.4'
        ? 'On 2.4 GHz a 40 MHz channel eats two of the three clean slots, so the 802.11n rules make it fall back to 20 MHz when it sees overlap, and many clients refuse 40 MHz here anyway. With neighbours around, 20 MHz is usually faster in practice.'
        : 'On 5 GHz, 80 MHz bonds four channels. There is far more room up here, so 80 MHz is the normal choice.',
      example: (c) => c ? `This radio runs <code>${v(c.width)} MHz</code>${c.sideband ? `, its second channel ${c.sideband === 'upper' ? 'below' : 'above'} the primary` : ''}.` : '',
    },
    'wifi.power': {
      title: 'Transmit power',
      what: 'The radio\'s output as a share of its maximum. Halving the power is -3 dB.',
      change: 'Lower power shrinks coverage. With two APs sharing an SSID, lowering the main router\'s 2.4 GHz power nudges upstairs clients to roam to the closer AP sooner. Clients still transmit back on their own power, so more AP power alone won\'t fix one-way range problems.',
      example: (c) => `This radio transmits at <code>${v(c && c.power)}%</code>.`,
    },
    'wifi.standard': {
      title: 'Wi-Fi standard',
      what: '802.11b/g/n is Wi-Fi 4 (2.4 GHz, up to 150 Mb/s per stream at 40 MHz). 802.11a/n/ac is Wi-Fi 5 (5 GHz, up to 433 Mb/s per stream at 80 MHz). The actual rate also depends on how many antennas (spatial streams) the device has.',
      change: 'Allowing old modes (802.11b) keeps very old gear working, but while one is connected everyone else\'s frames get wrapped in protection that lowers throughput.',
      example: (c) => `This radio runs <code>${v(c && c.standard)}</code> (${v(c && c.generation)}).`,
      effect: READING,
    },
    'wifi.security': {
      title: 'Wi-Fi security',
      what: 'WPA2-Personal and WPA3-Personal both encrypt the air with a key derived from your Wi-Fi password, protecting frames with AES. WPA3 uses the SAE handshake, which adds forward secrecy and removes WPA2\'s exposure to offline password guessing. "WPA2/WPA3 transition" runs both so older devices still connect.',
      change: 'The password is the main lever: on WPA2 a short or common passphrase is the weak point, so a long random one matters. WPA3-only is strongest but drops gear that predates it (common in IoT). Avoid WEP and plain WPA/TKIP — both are obsolete.',
      example: (c) => c ? `This radio uses <code>${v(c.security)}</code> with <code>${v(c.cipher)}</code>. ${/WPA2|WPA3/.test(c.security) ? 'Solid; a 16+ character passphrase (or WPA2/WPA3 transition) closes the common weakness while keeping older devices online.' : 'Consider moving to WPA2 or WPA3.'}` : '',
      effect: READING,
    },
    'wifi.pmf': {
      title: 'Protected management frames (802.11w)',
      what: 'Management frames (the ones that associate and, on older Wi-Fi, disconnect clients) are normally sent unauthenticated. 802.11w signs them so a device can tell a forged disconnect from a real one.',
      change: 'With PMF off, a nearby device can forge "disconnect" frames and knock clients off the network repeatedly. WPA3 requires PMF; WPA2 can enable it as "capable" (on for devices that support it) or "required".',
      example: (c) => `PMF here is <code>${v(c && c.pmf)}</code>.${c && c.pmf === 'off' ? ' Setting it to "capable" protects modern clients without dropping old ones.' : ''}`,
      effect: READING,
    },
    'wifi.wps': {
      title: 'WPS (one-touch pairing)',
      what: 'WPS lets a device join without the password, by an 8-digit PIN or a push-button that opens a ~2-minute window. The PIN method has a well-known design weakness and is widely recommended off; routers of one chipset often ship the same factory PIN.',
      change: 'Turning WPS off means devices join by entering the Wi-Fi password — the recommended setting. Push-button is lower-risk than PIN because the window is short and needs physical access to the router.',
      example: (c) => c && c.enabled
        ? `WPS is on here${c.defaultPin ? `, with the factory PIN <code>${v(c.pin)}</code>` : ''}. Turning it off is safer; you can still add devices with the password or a QR code.`
        : 'WPS is off here, so every device joins with the password.',
      effect: READING,
    },
    'wifi.guest': {
      title: 'Guest networks',
      what: 'Each radio can broadcast extra SSIDs (multiple BSSIDs on one radio). A guest SSID can be put on its own subnet with client isolation, so guests reach the internet but not your LAN devices.',
      change: 'Guests share the same radio and airtime as your main network. A separate guest SSID means you never hand out your main password, and you can turn it off without touching your own devices.',
      example: (c) => `This radio has ${v(c && c.slots)} spare SSID slots, ${v(c && c.used)} in use.`,
      effect: READING,
    },
    'wifi.neighbours': {
      title: 'Nearby networks',
      what: 'The access points the radio heard in its last background scan, with their channel and signal. Overlap on your channel is the usual cause of slow Wi-Fi in a block of flats.',
      change: 'Pick a channel the strong neighbours aren\'t on. On 2.4 GHz that means choosing whichever of 1, 6 or 11 is least crowded; on 5 GHz there is usually a free channel.',
      example: (c) => c && c.busiest != null ? `The busiest nearby channel is <code>${v(c.busiest)}</code>.` : 'No neighbours were seen in the last scan.',
      effect: READING,
    },

    /* ---------- Security ---------- */
    'sec.overview': {
      title: 'How this check-up works',
      what: 'The panel reads the router\'s firewall, remote-access, UPnP, DMZ, port-forward and Wi-Fi pages and rates each against a safe default. Nothing here is changed — it is a read-only check-up.',
      change: 'Red means act, orange means worth a look, blue is context, green is already fine. Fixes are made on the router\'s own page for now; this app only blocks, unblocks and speed-limits.',
      example: 'For example, "WPS on with factory PIN" is red because that combination is a known default weakness; turning WPS off clears it.',
      effect: READING,
    },

    /* ---------- Tools ---------- */
    'tool.ping': {
      title: 'Ping',
      what: 'The router sends ICMP echo requests to a host and times the replies. Because the router runs it, the result shows the path from your connection outward, without your own Wi-Fi or computer in the way.',
      change: 'Round-trip time is the latency to that host. Steady times mean a stable path; big swings (jitter) or lost packets show up as lag in calls and games. "100% packet loss" with a reachable network usually means that host drops ICMP, not that it is down.',
      example: 'Pinging 1.1.1.1 at ~17 ms with 0% loss is a healthy fibre path. 150+ ms or loss points at congestion or a problem upstream.',
      effect: READING,
    },
    'tool.traceroute': {
      title: 'Traceroute',
      what: 'The router sends packets with a rising hop limit (TTL). Each router on the path drops the packet when the limit hits zero and reports back, so each hop reveals one step toward the destination.',
      change: 'It shows where latency appears and where a path stops. The first hop is your ISP\'s gateway. A hop that times out but later ones answer just means that one router hides from traceroute — not a fault.',
      example: 'A trace to 1.1.1.1 that reaches it in 5 hops, climbing from ~1 ms to ~17 ms, is a short healthy path. A big jump at one hop is where the delay enters.',
      effect: READING,
    },
  };
})();
