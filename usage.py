"""Internet usage history for fun-router.

The router only keeps byte counters since it last booted (and per Wi-Fi device, only
while that device stays connected), so history has to be recorded here. The panel
samples the counters about once a minute and stores the *difference* since the last
sample in a local SQLite file. Everything is in local time.

Limits, stated on the page too: history only covers time while fun-router was running
and could read the router, and only devices on the router's own Wi-Fi have their own
counters. Everything else is the whole connection's total minus the Wi-Fi devices.
"""

import datetime as dt
import sqlite3
import threading
import time

WAN = 'wan'
MAX_BPS = 2.5e9          # a delta faster than this is a counter glitch, not traffic
KEEP_DAYS = 400          # prune older rows


def _midnight(day):
    return int(time.mktime(day.timetuple()))


def range_starts(now=None):
    """Local start times for today, this week (Monday) and this month."""
    now = dt.datetime.fromtimestamp(now or time.time())
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        'today': _midnight(today),
        'week': _midnight(today - dt.timedelta(days=today.weekday())),
        'month': _midnight(today.replace(day=1)),
    }


class UsageStore:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS usage (
                ts INTEGER NOT NULL, scope TEXT NOT NULL, secs INTEGER NOT NULL,
                down INTEGER NOT NULL, up INTEGER NOT NULL,
                reset INTEGER NOT NULL DEFAULT 0);  -- 1 = counter restarted; real duration unknown
            CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts, scope);
            CREATE TABLE IF NOT EXISTS coverage (ts INTEGER NOT NULL, secs INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS coverage_ts ON coverage(ts);
            CREATE TABLE IF NOT EXISTS optics (ts INTEGER NOT NULL, rx REAL, tx REAL);
            CREATE TABLE IF NOT EXISTS names (
                mac TEXT PRIMARY KEY, name TEXT, ip TEXT, band TEXT, seen INTEGER);
        ''')
        self.db.commit()
        self.last = {}           # scope -> (ts, down counter, up counter)
        self.boot_totals = None  # (down, up) since the router booted, from the latest sample
        self.last_sample = 0

    # --- Recording ---

    def record(self, now, wan, stations, names, optics=None):
        """Store one sample.

        wan: (bytes down, bytes up) counters for the whole connection, or None.
        stations: {mac: (bytes down, bytes up)} counters for router Wi-Fi clients.
        names: {mac: (name, ip, band)} so old devices keep a label after they leave.
        optics: (rx dBm, tx dBm) or None.
        """
        rows = []
        gap = None
        if wan:
            self.boot_totals = wan
            gap = self._delta(WAN, now, wan, rows)
        for mac, counters in stations.items():
            self._delta(mac, now, counters, rows)
        with self.lock:
            if rows:
                self.db.executemany('INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?)', rows)
            if gap:
                self.db.execute('INSERT INTO coverage VALUES (?, ?)', (now, gap))
            for mac, (name, ip, band) in names.items():
                self.db.execute('INSERT INTO names VALUES (?, ?, ?, ?, ?) ON CONFLICT(mac) DO UPDATE SET '
                                'name = COALESCE(excluded.name, names.name), ip = excluded.ip, '
                                'band = COALESCE(excluded.band, names.band), seen = excluded.seen',
                                (mac, name, ip, band, now))
            if optics and optics[0] is not None:
                self.db.execute('INSERT INTO optics VALUES (?, ?, ?)', (now, optics[0], optics[1]))
            self.db.commit()
        self.last_sample = now

    def _delta(self, scope, now, counters, rows):
        """Append the usage since this scope's previous sample. Returns the seconds covered."""
        prev = self.last.get(scope)
        self.last[scope] = (now, counters[0], counters[1])
        if not prev:
            return None  # first sight: this is only a baseline
        secs = max(1, int(now - prev[0]))
        down, up = counters[0] - prev[1], counters[1] - prev[2]
        reset = down < 0 or up < 0
        if reset:
            # The counter restarted (router reboot, or the device reconnected); what it
            # shows now was all used since then.
            down, up = max(counters[0], 0), max(counters[1], 0)
        if not reset and (down + up) * 8 / secs > MAX_BPS:
            return None
        if down or up:
            rows.append((int(now), scope, secs, down, up, int(reset)))
        return secs

    def prune(self, now):
        cutoff = int(now - KEEP_DAYS * 86400)
        with self.lock:
            for table in ('usage', 'coverage', 'optics'):
                self.db.execute('DELETE FROM %s WHERE ts < ?' % table, (cutoff,))
            self.db.commit()

    # --- Queries ---

    def _rows(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def summary(self, now=None):
        """Everything the Usage page draws, for today / this week / this month."""
        now = int(now or time.time())
        starts = range_starts(now)
        first = self._rows('SELECT MIN(ts) FROM coverage')[0][0]
        names = {mac: {'name': name, 'ip': ip, 'band': band}
                 for mac, name, ip, band, _ in self._rows('SELECT * FROM names')}
        out = {'now': now, 'recordingSince': first, 'starts': starts, 'ranges': {}}
        for key, start in starts.items():
            out['ranges'][key] = self._range(key, start, now, names)
        out['heatmap'] = self._heatmap(now)
        out['optics'] = self._optics(now - 7 * 86400)
        out['daily'] = self._buckets(now - 30 * 86400, now, 'day')
        return out

    def _range(self, key, start, now, names):
        wan = self._rows('SELECT COALESCE(SUM(down), 0), COALESCE(SUM(up), 0) FROM usage '
                         'WHERE scope = ? AND ts > ?', (WAN, start))[0]
        covered = self._rows('SELECT COALESCE(SUM(secs), 0) FROM coverage WHERE ts > ?', (start,))[0][0]
        peak = self._rows('SELECT ts, down * 8.0 / secs, up * 8.0 / secs FROM usage WHERE scope = ? AND ts > ? AND reset = 0 '
                          'ORDER BY (down + up) * 1.0 / secs DESC LIMIT 1', (WAN, start))
        devices = []
        wifi_down = wifi_up = 0
        for mac, down, up in self._rows('SELECT scope, SUM(down), SUM(up) FROM usage WHERE scope != ? AND ts > ? '
                                        'GROUP BY scope', (WAN, start)):
            info = names.get(mac, {})
            devices.append({'mac': mac, 'name': info.get('name'), 'ip': info.get('ip'), 'band': info.get('band'),
                            'down': down, 'up': up})
            wifi_down += down
            wifi_up += up
        devices.sort(key=lambda d: d['down'] + d['up'], reverse=True)
        bucket = 'hour' if key == 'today' else 'day'
        return {
            'start': start,
            'down': wan[0], 'up': wan[1],
            'coveredSecs': covered, 'elapsedSecs': max(1, now - start),
            'peak': {'ts': peak[0][0], 'downBps': peak[0][1], 'upBps': peak[0][2]} if peak else None,
            'devices': devices,
            # Station counters also include local (LAN) traffic, so this can dip below zero.
            'otherDown': max(0, wan[0] - wifi_down), 'otherUp': max(0, wan[1] - wifi_up),
            'buckets': self._buckets(start, self._range_end(key, start), bucket),
        }

    @staticmethod
    def _range_end(key, start):
        day = dt.datetime.fromtimestamp(start)
        if key == 'today':
            return start + 86400
        if key == 'week':
            return _midnight(day + dt.timedelta(days=7))
        nxt = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return _midnight(nxt)

    def _buckets(self, start, end, size):
        """[{t, down, up, covered}] per local hour or day between start and end."""
        edges = []
        cur = dt.datetime.fromtimestamp(start)
        cur = cur.replace(minute=0, second=0, microsecond=0) if size == 'hour' else \
            cur.replace(hour=0, minute=0, second=0, microsecond=0)
        step = dt.timedelta(hours=1) if size == 'hour' else dt.timedelta(days=1)
        while _midnight(cur) < end:
            edges.append(_midnight(cur))
            cur += step
        edges.append(_midnight(cur))
        out = [{'t': t, 'down': 0, 'up': 0, 'covered': 0} for t in edges[:-1]]
        if not out:
            return out
        rows = self._rows('SELECT ts, down, up FROM usage WHERE scope = ? AND ts > ? AND ts <= ?',
                          (WAN, edges[0], edges[-1]))
        cov = self._rows('SELECT ts, secs FROM coverage WHERE ts > ? AND ts <= ?', (edges[0], edges[-1]))

        def index(ts):
            # Samples are stamped at the END of the interval they cover.
            lo, hi = 0, len(edges) - 1
            while lo < hi - 1:
                mid = (lo + hi) // 2
                if edges[mid] < ts:
                    lo = mid
                else:
                    hi = mid
            return lo

        for ts, down, up in rows:
            b = out[index(ts)]
            b['down'] += down
            b['up'] += up
        for ts, secs in cov:
            out[index(ts)]['covered'] += secs
        return out

    def _heatmap(self, now):
        """[weekday 0=Mon][hour] -> bytes, over the last 28 days."""
        grid = [[0] * 24 for _ in range(7)]
        for ts, down, up in self._rows('SELECT ts, down, up FROM usage WHERE scope = ? AND ts > ?',
                                       (WAN, now - 28 * 86400)):
            moment = dt.datetime.fromtimestamp(ts - 1)
            grid[moment.weekday()][moment.hour] += down + up
        return grid

    def _optics(self, start):
        rows = self._rows('SELECT ts, rx, tx FROM optics WHERE ts > ? ORDER BY ts', (start,))
        step = max(1, len(rows) // 400)  # at most ~400 points
        return [{'t': ts, 'rx': rx, 'tx': tx} for ts, rx, tx in rows[::step]]
