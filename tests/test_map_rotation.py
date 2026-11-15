import json
import unittest
from io import BytesIO
from unittest.mock import patch

from matchlog.map_rotation import FALLBACK_SOURCE, SOURCE, MapRotation


class MapRotationTests(unittest.TestCase):
    def test_partial_feed_gets_public_ranked_and_next_pubs(self):
        feed = {'v': 1, 'now': 200, 'modes': {'mixtape': {'current': {
            'map': 'Thunderdome', 'start': 190, 'end': 220, 'event': 'TDM',
        }}}}
        page = """
        <div class="row curmap-overview-card">
          <h2>BR Ranked</h2><h2>Storm Point</h2>
          <div id="timer" data-start="150" data-end="230"></div>
          <h4>E-District</h4><p><span data-tz="230">00:00</span>-<span data-tz="290">01:00</span></p>
        </div>
        <div class="row curmap-overview-card">
          <h2>BR Pubs</h2><h2></h2><div id="timer" data-start="" data-end=""></div>
          <h4>Broken Moon</h4><p><span data-tz="210">00:00</span>-<span data-tz="300">01:00</span></p>
        </div>
        """
        responses = [BytesIO(json.dumps(feed).encode()), BytesIO(page.encode())]
        with patch('matchlog.map_rotation.urlopen', side_effect=responses) as fetch:
            with patch('matchlog.map_rotation.time.time', return_value=200):
                rotation = MapRotation().fetch()
        self.assertEqual([call.args[0].full_url for call in fetch.call_args_list], [SOURCE, FALLBACK_SOURCE])
        self.assertFalse(rotation['error'])
        modes = rotation['modes']
        self.assertEqual(modes['ranked']['current']['map'], 'Storm Point')
        self.assertEqual(modes['ranked']['next']['start'], 230)
        self.assertEqual(modes['mixtape']['current']['event'], 'TDM')
        self.assertNotIn('current', modes['pubs'])
        self.assertEqual(modes['pubs']['next']['map'], 'Broken Moon')
