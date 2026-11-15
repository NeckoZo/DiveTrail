"""Check that the lightweight poll does not hide real record or clock changes."""

from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from support import Api


class DashboardPoll(unittest.TestCase):
    def test_live_capture_status_does_not_rebuild_unchanged_records(self):
        with tempfile.TemporaryDirectory() as root:
            api = Api(root, desktop=False)
            with closing(api._store.db), patch("app.time.time", return_value=120):
                first = api.poll_state()
                self.assertIn("state", first)
                api._runtime["hotkey_error"] = "F8 暂不可用"
                with patch.object(api, "_state_snapshot", side_effect=AssertionError("unexpected full snapshot")):
                    second = api.poll_state(first["revision"])
                self.assertEqual(second["live"]["runtime"]["hotkey_error"], "F8 暂不可用")
                self.assertNotIn("state", second)

    def test_record_change_and_rolling_clock_both_get_full_snapshots(self):
        with tempfile.TemporaryDirectory() as root:
            api = Api(root, desktop=False)
            with closing(api._store.db), patch("app.time.time", return_value=120) as clock:
                first = api.poll_state()
                api._store.create_manual({
                    "time": "2026-11-05T23:00:00+08:00", "mode": "ranked", "placement": 7,
                    "players": [{"name": "PlayerOne"}],
                })
                second = api.poll_state(first["revision"])
                self.assertEqual(len(second["state"]["matches"]), 1)
                clock.return_value = 135
                third = api.poll_state(second["revision"])
                self.assertEqual(len(third["state"]["matches"]), 1)
                self.assertNotEqual(third["revision"], second["revision"])
