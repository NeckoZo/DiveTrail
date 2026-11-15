"""Bound the expensive frames saved from a continuous game screen."""

from collections import deque
from dataclasses import dataclass
from threading import Lock

from PIL import Image


HUD_INTERVALS = (0, 4, 60, 120, 180, 240, 300, 390)
SETTLEMENT_INTERVALS = (0, 1.4, 2.6, 4.5, 8, 15, 30)
NAVIGATION_INTERVALS = (0, 5)


@dataclass(frozen=True)
class HudCandidate:
    scope: str
    round_id: str
    captured_at: float
    fingerprint: bytes
    size: tuple
    parts: tuple
    owner_key: bytes=b''

    def canvas(self):
        image=Image.new('RGB',self.size,'black')
        for origin,part in self.parts:
            image.paste(part,origin)
        return image


class HudTail:
    """Keep current ROIs in memory and hand each finished round off once."""

    def __init__(self):
        self._lock=Lock()
        self._key=None
        self._frames=deque(maxlen=3)
        self._sealed=deque(maxlen=8)
        self._closed=deque(maxlen=32)
        self._reading=None
        self._frozen=False
        self._probe=None
        self._claimed_probe=None
        self._probe_at=-float('inf')

    def clear(self):
        with self._lock:
            self._key=None
            self._frames.clear()
            self._sealed.clear()
            self._closed.clear()
            self._reading=None
            self._frozen=False
            self._probe=None
            self._claimed_probe=None
            self._probe_at=-float('inf')

    def screen(self, scope, round_id, hint):
        with self._lock:
            if self._key is not None and self._key[0]!=scope:
                self._key=None
                self._frames.clear()
                self._probe=None
                self._claimed_probe=None
            next_key=(scope,round_id)
            ended=hint in ('lobby','settlement') or hint in ('intro','hud') and round_id and next_key!=self._key
            if ended and self._key:
                self._closed.append(self._key)
                if self._frames:self._sealed.append((self._key,tuple(reversed(self._frames))[:2]))
                self._key=None
                self._frames.clear()
                self._probe=None
                self._claimed_probe=None
            if hint=='hud' and round_id and self._key!=next_key and next_key not in self._closed:
                self._key=next_key
                self._frozen=False
                self._probe_at=-float('inf')
            if hint=='spectator' and self._key:
                self._frozen=True
                self._probe=None
                self._claimed_probe=None

    def freeze(self, scope, round_id, since):
        with self._lock:
            if self._key==(scope,round_id):
                self._frozen=True
                self._claimed_probe=None
                self._frames=deque((frame for frame in self._frames if frame.captured_at<since),maxlen=3)

    def wants_frame(self, scope, round_id, now):
        with self._lock:
            return self._key==(scope,round_id) and (not self._frozen or now-self._probe_at>=10)

    def round_id(self, scope):
        with self._lock:
            return self._key[1] if self._key and self._key[0]==scope else None

    def rebind(self, scope, previous_id, round_id):
        with self._lock:
            if self._key==(scope,previous_id):
                self._key=(scope,round_id)
                self._probe=None
                self._claimed_probe=None

    def _append(self, candidate):
        if (self._frames and self._frames[-1].fingerprint==candidate.fingerprint
                and self._frames[-1].owner_key==candidate.owner_key):
            self._frames.pop()
        self._frames.append(candidate)

    def remember(self, candidate, now):
        with self._lock:
            if self._key!=(candidate.scope,candidate.round_id):return False
            if self._frozen:
                self._probe=candidate
                self._probe_at=now
                return False
            self._append(candidate)
            return True

    def claim_probe(self):
        with self._lock:
            candidate=self._probe
            self._probe=None
            self._claimed_probe=candidate
            return candidate

    def resume(self, candidate):
        with self._lock:
            if (self._key!=(candidate.scope,candidate.round_id) or not self._frozen
                    or self._claimed_probe is not candidate):return False
            self._frozen=False
            self._probe=None
            self._claimed_probe=None
            self._append(candidate)
            return True

    def claim_finished(self):
        with self._lock:
            if not self._sealed:return ()
            self._reading,candidates=self._sealed.popleft()
            return candidates

    def finish(self):
        with self._lock:self._reading=None

    def pending(self, scope, round_id):
        with self._lock:
            key=(scope,round_id)
            return key==self._reading or any(item[0]==key for item in self._sealed)


def due(sample, now, intervals):
    count = sample.get('count', 0)
    return count < len(intervals) and now >= sample.get('at', 0) + intervals[count]


def record_sample(sample, now):
    sample['count'] = sample.get('count', 0) + 1
    sample['at'] = now


def settlement_page(top):
    """Use the selected salmon underline to recognize a tab change cheaply."""
    strip = top.convert('RGB').resize((256, 56), Image.Resampling.BILINEAR)
    for y in range(21, 52):
        pink = [x for x in range(65, 192)
                if (color := strip.getpixel((x, y)))[0] > 175
                and 98 < color[1] < 216 and 72 < color[2] < 210
                and color[0] - color[1] >= 24 and color[1] - color[2] >= 3]
        if len(pink) >= 14 and pink[-1] - pink[0] <= len(pink) + 4:
            return round(pink[len(pink) // 2] / 12)
    return 'detail'
