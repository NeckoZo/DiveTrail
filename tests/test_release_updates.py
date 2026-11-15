import unittest
from unittest.mock import Mock

import requests

from matchlog.release_updates import RELEASE_API, ReleaseUpdates


def response(tag='v1.10.0', http=200, **fields):
    result = Mock(status_code=http)
    result.json.return_value = {'tag_name': tag, **fields}
    return result


class ReleaseUpdatesTests(unittest.TestCase):
    def test_newer_release_notifies_and_cache_isolated(self):
        now = Mock(return_value=100)
        get = Mock(return_value=response())
        updater = ReleaseUpdates('1.9.9', get=get, clock=now)

        result = updater.check()
        self.assertEqual(result, {'status': 'available', 'version': '1.10.0'})
        result['status'] = 'modified'
        now.return_value = 3699
        self.assertEqual(updater.check()['status'], 'available')
        get.assert_called_once_with(
            RELEASE_API, timeout=(2, 4), allow_redirects=False,
            headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'DiveTrail'},
        )

        now.return_value = 3700
        get.return_value = response('v1.9.9')
        self.assertEqual(updater.check(), {'status': 'current', 'version': '1.9.9'})
        self.assertEqual(get.call_count, 2)

    def test_invalid_and_unpublished_versions_do_not_notify(self):
        for entry in (
            response('v1.10.0-rc1'), response('1.10.0\n'),
            response(draft=True), response(prerelease=True),
            response(http=404), response(http=302),
        ):
            with self.subTest(response=entry):
                self.assertEqual(ReleaseUpdates('1.9.9', get=Mock(return_value=entry)).check(),
                                 {'status': 'unavailable'})

    def test_timeout_does_not_block_startup_and_retries_after_short_cache(self):
        now = Mock(return_value=100)
        get = Mock(side_effect=[requests.Timeout(), response('v1.0.0')])
        updater = ReleaseUpdates('1.0.1', get=get, clock=now)

        self.assertEqual(updater.check(), {'status': 'unavailable'})
        now.return_value = 699
        self.assertEqual(updater.check(), {'status': 'unavailable'})
        get.assert_called_once()
        now.return_value = 700
        self.assertEqual(updater.check(), {'status': 'current', 'version': '1.0.0'})
        self.assertEqual(get.call_count, 2)
