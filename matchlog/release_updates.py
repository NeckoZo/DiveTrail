"""Check the public stable release without downloading executable code."""

import re
from threading import Lock
import time

import requests


REPOSITORY = 'NeckoZo/DiveTrail'
WEBSITE = 'https://neckozo.github.io/DiveTrail/'
RELEASES = f'https://github.com/{REPOSITORY}/releases'
RELEASE_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
VERSION = re.compile(r'v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})', re.ASCII)


def version_parts(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    return tuple(map(int, match.groups())) if match else None


class ReleaseUpdates:
    def __init__(self, version, get=None, clock=None):
        self._installed = version_parts(version)
        self._get = get or requests.get
        self._clock = clock or time.monotonic
        self._lock = Lock()
        self._expires = float('-inf')
        self._result = {'status': 'unavailable'}

    def check(self):
        with self._lock:
            now = self._clock()
            if now < self._expires:
                return self._result.copy()
            try:
                response = self._get(
                    RELEASE_API, timeout=(2, 4), allow_redirects=False,
                    headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'DiveTrail'},
                )
                if response.status_code != 200:
                    raise ValueError('Latest release unavailable')
                release = response.json()
                newest = version_parts(release.get('tag_name')) if isinstance(release, dict) else None
                if not newest or not self._installed or release.get('draft') or release.get('prerelease'):
                    raise ValueError('Invalid stable release')
                version = '.'.join(map(str, newest))
                self._result = {'status': 'available' if newest > self._installed else 'current', 'version': version}
            except (requests.RequestException, ValueError):
                self._result = {'status': 'unavailable'}
            self._expires = self._clock() + (600 if self._result['status'] == 'unavailable' else 3600)
            return self._result.copy()
