"""A local support export preserves relationships without copying private values."""

from contextlib import closing
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import Mock

from PIL import Image

from matchlog.diagnostic_export import build_report, write_report
from support import Api


class DiagnosticExport(unittest.TestCase):
    def test_recent_rows_and_trace_share_anonymous_references(self):
        with tempfile.TemporaryDirectory(prefix='private-machine-') as folder:
            root = Path(folder)
            api = Api(root, desktop=False)
            with closing(api._store.db):
                store, journal = api._store, api._journal
                store.configure({'self_name': 'PrivatePlayer'})
                now = time.time()
                rid = journal.note_screen('hud', now - 60)
                journal.bind_game_key(rid, 'private-session-SID', now - 60)
                image = Image.new('RGB', (64, 32), 'green')
                jobs = [journal.enqueue(image, 'settlement', rid, timestamp=now - at) for at in (15, 10)]
                journal.finish(jobs[0], 'failed', "private-local-path 'TextRecOutput' has no attribute 'boxes'")
                api._diagnostics.add('reading', phase='settlement', job=jobs[0], round_id=rid)

                archive = root / 'captures' / 'private-filename.png'
                archive.parent.mkdir()
                image.save(archive)
                match = store.ingest({
                    'kind': 'summary', 'mode': 'ranked', 'placement': 9,
                    'players': [{'name': 'PrivatePlayer', 'kills': 1, 'damage': 111},
                                {'name': 'PrivateFriend', 'damage': 222}],
                }, archive, round_id=rid, timestamp=now - 20)
                store.ingest({'kind': 'rank', 'delta': -20}, archive, digest='demo-rank', round_id=rid, timestamp=now - 19)
                old = journal.note_screen('intro', now - 90_000)
                journal.enqueue(image, 'manual', old, manual=True, timestamp=now - 90_000)
                report = build_report(
                    store, api._diagnostics, {'game_size': (2560, 1440), 'size': [1920, 1080], 'message': 'private-error'},
                    {'device_pixel_ratio': 1.25, 'viewport_css': [1200, 700], 'screen_css': ['private-dpi', 1400]},
                    version='0.11.1', now=now + 1,
                )
                self.assertEqual(report['window_hours'], 24)
                self.assertEqual(len(report['jobs']), 2)
                first = next(row for row in report['jobs'] if row['status'] == 'failed')
                self.assertEqual(first['error_category'], 'ocr_output_shape')
                self.assertEqual(first['job'], report['current_run']['events'][0]['job'])
                self.assertEqual(first['round'], report['matches'][0]['round'])
                self.assertEqual(report['matches'][0]['displayed_rp'], -20)
                self.assertEqual(report['matches'][0]['own']['damage'], 111)
                self.assertEqual(first['file'], report['jobs'][0]['file'])
                self.assertEqual(report['file_counts'], {'queue': 1, 'summary': 1})
                queue_file = next(row for row in report['files'] if row['kind'] == 'queue')
                self.assertEqual(queue_file['pixels'], [64, 32])
                self.assertGreater(queue_file['bytes'], 0)
                self.assertEqual(report['environment']['game_client_px'], [2560, 1440])
                self.assertEqual(report['environment']['last_ocr_input_px'], [1920, 1080])
                self.assertEqual(report['environment']['webview']['device_pixel_ratio'], 1.25)
                self.assertIsNone(report['environment']['webview']['screen_css'])
                target = root / 'shared-diagnostics.json'
                write_report(target, report)
                exported = target.read_text(encoding='utf-8')
                for secret in ('PrivatePlayer', 'PrivateFriend', 'private-', str(root), jobs[0], rid, match):
                    self.assertNotIn(secret, exported)
                self.assertEqual(json.loads(exported), report)

    def test_cancelled_save_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            api = Api(Path(folder), desktop=False)
            with closing(api._store.db):
                api._window = Mock()
                api._window.create_file_dialog.return_value = None
                self.assertEqual(api.export_capture_diagnostics(), {'ok': False})
                self.assertEqual(list(Path(folder).glob('*.json')), [])
                api._window.create_file_dialog.return_value = (str(Path(folder) / 'selected'),)
                self.assertEqual(api.export_capture_diagnostics(), {'ok': True})
                report = json.loads((Path(folder) / 'selected.json').read_text(encoding='utf-8'))
                self.assertEqual(report['format'], 'matchlog.capture-diagnostics')
