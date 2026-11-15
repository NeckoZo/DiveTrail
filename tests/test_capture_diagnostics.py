"""Verify shared diagnostics are useful without exposing capture contents."""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from matchlog.capture_diagnostics import CaptureDiagnostics
from support import Api


class Diagnostics(unittest.TestCase):
    def test_report_keeps_only_recent_events_and_anonymizes_job_and_round_ids(self):
        trail = CaptureDiagnostics(limit=2)
        trail.add('no_game')
        trail.add('queued', phase='settlement', job='private-job-id', round_id='private-round-id')
        trail.add('unstable', phase='summary', job='private-job-id', round_id='private-round-id')
        trail.add('C:/Users/' + 'PrivateName/secret.png')
        text = trail.report()
        self.assertNotIn('等待 Apex 前台', text)
        self.assertNotIn('private-', text)
        self.assertNotIn('PrivateName', text)
        self.assertEqual(text.count('截图 #1 / 场次 #2'), 2)
        self.assertIn('等待另一帧确认', text)

    def test_repeated_background_conditions_are_throttled(self):
        trail = CaptureDiagnostics()
        with patch('matchlog.capture_diagnostics.time.monotonic', side_effect=[1, 10, 61]):
            for _ in range(3):
                trail.add('no_game', every=60)
        self.assertEqual(trail.report().count('等待 Apex 前台'), 2)

    def test_actual_queued_job_reports_unrecognized_frame_and_unknown_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            api = Api(Path(folder), desktop=False)
            with closing(api._store.db):
                frame = Image.new('RGB', (20, 20), 'green')
                job_id = api._journal.enqueue(frame, 'manual', manual=True)
                api._ocr.read = Mock(return_value={
                    'kind': 'unknown', 'size': [20, 20],
                    'tokens': [{'text': 'private-message'}], 'warnings': ['private-path'],
                })
                api._run_capture_job(api._journal.get(job_id))
                api._parsed_frames.clear()
                second_id = api._journal.enqueue(frame, 'manual', manual=True)
                api._ocr.read = Mock(return_value={
                    'kind': 'summary', 'size': [20, 20], 'players': [{'name': 'PrivatePlayer'}],
                })
                api._run_capture_job(api._journal.get(second_id))
                report = api.capture_diagnostics()
                self.assertIn('开始识别已保存的截图', report)
                self.assertIn('没有识别出画面类型', report)
                self.assertIn('无法确认是本人', report)
                self.assertIn('截图已保留', report)
                self.assertNotIn('PrivatePlayer', report)
                self.assertNotIn('private-', report)
                self.assertNotIn(job_id, report)
