"""Serialize quiet end-of-match cues off the capture and OCR threads."""

from array import array
from collections import OrderedDict
from functools import lru_cache
from io import BytesIO
import logging
from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
import time
import wave

from matchlog.config import SETTLEMENT_TONES


SHORT_TERM_RMS = 32768 * 10**(-29 / 20)
PEAK_LIMIT = 32768 * 10**(-14 / 20)


@lru_cache(maxsize=8)
def _quiet_wav(path, modified_ns, size):
    """Match the loudest 50 ms of each cue and cap individual peaks."""
    with wave.open(str(path), 'rb') as source:
        channels = source.getnchannels()
        frame_rate = source.getframerate()
        frames = source.getnframes()
        if source.getsampwidth() != 2 or not 0 < frames <= 5 * frame_rate:
            raise ValueError('Cues must be short, 16-bit PCM WAV files')
        samples = array('h', source.readframes(frames))
    peak = max(map(abs, samples), default=0)
    if not peak:
        raise ValueError('The cue has no audible samples')
    window = min(len(samples), max(1, frame_rate * channels // 20))
    stride = max(1, window // 2)
    rms = max(
        sum(sample * sample for sample in samples[start:start + window])
        for start in range(0, len(samples), stride)
    )
    rms = (rms / window)**0.5
    gain = min(1, SHORT_TERM_RMS / rms, PEAK_LIMIT / peak)
    result = BytesIO()
    with wave.open(result, 'wb') as output:
        output.setparams((channels, 2, frame_rate, 0, 'NONE', 'not compressed'))
        output.writeframes(array('h', (round(sample * gain) for sample in samples)).tobytes())
    return result.getvalue()


class SettlementSounds:
    def __init__(self, data_root, bundled):
        self._custom = Path(data_root) / 'sounds'
        self._bundled = Path(bundled)
        self._queue = Queue(maxsize=4)
        self._seen = OrderedDict()
        self._lock = Lock()
        self._thread = None

    def play(self, tone, *, round_id=None, event=None):
        if tone not in SETTLEMENT_TONES:
            return False
        key = (round_id, event) if round_id is not None else None
        with self._lock:
            if key is not None and key in self._seen:
                return False
            try:
                self._queue.put_nowait((tone, time.monotonic()))
            except Full:
                return False
            if key is not None:
                self._seen[key] = None
                if len(self._seen) > 256:
                    self._seen.popitem(last=False)
            if self._thread is None:
                self._thread = Thread(target=self._run, daemon=True, name='settlement-sounds')
                self._thread.start()
            return True

    def _clip(self, tone):
        for folder in (self._custom, self._bundled):
            path = folder / (tone + '.wav')
            try:
                version = path.stat()
                return _quiet_wav(path, version.st_mtime_ns, version.st_size)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, EOFError, wave.Error):
                logging.debug('Could not load settlement cue %s', tone, exc_info=True)
        return None

    def _run(self):
        import winsound

        while True:
            tone, queued_at = self._queue.get()
            if time.monotonic() - queued_at > 4:
                continue
            clip = self._clip(tone)
            if clip is None:
                continue
            try:
                winsound.PlaySound(clip, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            except RuntimeError:
                logging.debug('settlement sound unavailable', exc_info=True)
