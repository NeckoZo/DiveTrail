"""Fetch public Apex rotation metadata without sending local match data."""

from html import unescape
import json
import math
import re
import threading
import time
from urllib.request import Request, urlopen

SOURCE = 'https://stats.evotracker.cn/v1/maps'
FALLBACK_SOURCE = 'https://apexlegendsstatus.com/current-map'
USER_AGENT = 'MatchLog/0.19 map rotation'
MODES = ('ranked', 'pubs', 'mixtape')


def parse_rotation(data):
    if not isinstance(data, dict) or data.get('v') != 1:
        raise ValueError('Unexpected rotation response')
    now = data.get('now')
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise ValueError('Invalid server clock')
    modes = {}
    for mode in MODES:
        slots = (data.get('modes') or {}).get(mode)
        if not isinstance(slots, dict):
            continue
        valid = {}
        for phase in ('current', 'next'):
            item = slots.get(phase)
            if not isinstance(item, dict):
                continue
            name, start, end = item.get('map'), item.get('start'), item.get('end')
            if (not isinstance(name, str) or not 0 < len(name.strip()) <= 72
                    or type(start) is not int or type(end) is not int
                    or not 0 < start < end or end - start > 7 * 86400):
                continue
            event = item.get('event')
            valid[phase] = {'map': name.strip(), 'start': start, 'end': end,
                            'event': event[:48] if isinstance(event, str) else None}
        if valid:
            modes[mode] = valid
    if not modes:
        raise ValueError('Empty rotation')
    return now, modes


def parse_public_rotation(page, now):
    """Read the timestamps published in Apex Legends Status's overview cards."""
    headings = {'BR Pubs': 'pubs', 'BR Ranked': 'ranked', 'Mixtape': 'mixtape'}
    modes = {}
    cards = re.split(r'<div[^>]*class="[^"]*\bcurmap-overview-card\b[^"]*"[^>]*>', page)[1:]
    for card in cards:
        titles = re.findall(r'<h2[^>]*>\s*([^<]*?)\s*</h2>', card)
        mode = headings.get(titles[0]) if titles else None
        if not mode:
            continue
        entries = []
        if len(titles) > 1 and titles[1]:
            interval = re.search(r'data-start="(\d+)"\s+data-end="(\d+)"', card)
            if interval:
                entries.append((titles[1], *interval.groups()))
        entries.extend(re.findall(r'<h4[^>]*>\s*([^<]+?)\s*</h4>\s*<p>\s*'
                                  r'<span data-tz="(\d+)">[^<]*</span>'
                                  r'[^\n]*?<span data-tz="(\d+)">[^<]*</span>\s*</p>', card))
        for title, start, end in entries:
            start, end = int(start), int(end)
            if not 0 < start < end or end <= now:
                continue
            name = unescape(title).strip()
            event = None
            if mode == 'mixtape':
                name, _, event = name.partition(' - ')
            phase = 'current' if start <= now else 'next'
            modes.setdefault(mode, {}).setdefault(phase, {
                'map': name, 'start': start, 'end': end, 'event': event or None,
            })
    return parse_rotation({'v': 1, 'now': now, 'modes': modes})[1]


class MapRotation:
    def __init__(self):
        self._lock = threading.Lock()
        self._modes = {}
        self._offset = 0
        self._last_attempt = -float('inf')
        self._error = False

    def fetch(self):
        with self._lock:
            at = time.monotonic()
            if at - self._last_attempt >= 15:
                self._last_attempt = at
                modes = {}
                try:
                    req = Request(SOURCE, headers={'User-Agent': USER_AGENT})
                    with urlopen(req, timeout=5) as response:
                        data = json.loads(response.read(16384))
                    now, modes = parse_rotation(data)
                    self._offset = now - time.time()
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                if any(not modes.get(mode, {}).get('current') for mode in MODES):
                    try:
                        req = Request(FALLBACK_SOURCE, headers={'User-Agent': USER_AGENT})
                        with urlopen(req, timeout=5) as response:
                            page = response.read(256 * 1024).decode('utf-8')
                        fallback = parse_public_rotation(page, time.time() + self._offset)
                        for mode, slots in fallback.items():
                            modes[mode] = {**slots, **modes.get(mode, {})}
                    except (OSError, ValueError, TypeError):
                        pass
                self._error = not modes
                if modes:
                    self._modes = modes
            return {'modes': self._modes, 'now': time.time() + self._offset, 'error': self._error}
