"""Only an enrolled admin using the CloudBase CLI can export diagnostic logs."""
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from matchlog.cloud_log_admin import CloudLogAdmin, OWNER_CONFIG, enroll, write_export


class CloudLogAdminTests(unittest.TestCase):
    def test_enrolled_machine_and_pin_fallback_require_local_cli(self):
        pin = str(secrets.randbelow(1_000_000)).zfill(6)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch('matchlog.cloud_log_admin.device_fingerprint', return_value='enrolled-machine') as machine,
            patch('matchlog.cloud_log_admin._cli', return_value=['test-cli']) as cli,
        ):
            admin = CloudLogAdmin(directory, directory)
            self.assertFalse(admin.status()['available'])
            with self.assertRaisesRegex(ValueError, '已登记'):
                admin.authorize(pin)
            enroll(directory, pin)
            config = json.loads((Path(directory) / OWNER_CONFIG).read_text(encoding='utf-8'))
            self.assertEqual(set(config), {'device', 'pin_salt', 'pin_digest'})
            self.assertEqual(len(config['pin_digest']), 64)
            self.assertEqual(admin.status(), {'available': True, 'requires_pin': False})
            self.assertEqual(admin.authorize(None), ['test-cli'])
            machine.return_value = 'other-machine'
            self.assertEqual(admin.status(), {'available': True, 'requires_pin': True})
            with self.assertRaisesRegex(ValueError, '不正确'):
                admin.authorize(None)
            self.assertEqual(admin.authorize(pin), ['test-cli'])
            cli.return_value = None
            self.assertFalse(admin.status()['available'])
            with self.assertRaisesRegex(ValueError, 'CloudBase CLI'):
                admin.authorize(pin)

    def test_paginates_without_exporting_anonymous_login_ids(self):
        first_receipt, second_receipt = 'a' * 64, 'b' * 64

        def row(owner, receipt, index, total):
            return [owner, receipt, index, total, '24h', '0.19.1', '2026-11-11',
                    '[[1794270000000,"reading","rank",null,2]]', '反馈说明' if index == 0 else None]

        calls = []
        pages = iter([
            [['1794271000.400']],
            [row("admin'user", first_receipt, 0, 2), row("admin'user", first_receipt, 1, 2)],
            [row('second-owner', second_receipt, 0, 1)],
        ])

        def query(sql):
            calls.append(sql)
            return next(pages)

        with tempfile.TemporaryDirectory() as directory, patch('matchlog.cloud_log_admin.PAGE_SIZE', 2):
            target = Path(directory) / 'export.jsonl'
            self.assertEqual(write_export(target, query), {'ok': True, 'reports': 2, 'batches': 3, 'events': 3})
            raw = target.read_text(encoding='utf-8')
            lines = list(map(json.loads, raw.splitlines()))
            self.assertEqual([line['report'] for line in lines[1:-1]], [1, 1, 2])
            self.assertEqual(lines[1]['feedback'], '反馈说明')
            self.assertNotIn('feedback', lines[2])
            self.assertTrue(lines[-1]['complete_export'])
            self.assertNotIn("admin'user", raw)
            self.assertNotIn('second-owner', raw)
            self.assertNotIn("admin'user", calls[-1])
            self.assertIn("decode('61646d696e2775736572', 'hex')", calls[-1])
            self.assertIn('received_at <= to_timestamp(1794271000.400)', calls[-1])

    def test_failed_page_keeps_existing_export(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'export.jsonl'
            target.write_text('previous export', encoding='utf-8')

            def query(sql):
                if 'clock_timestamp' in sql:
                    return [['1794271000.400']]
                raise ValueError('connection unavailable')

            with self.assertRaisesRegex(ValueError, 'connection unavailable'):
                write_export(target, query)
            self.assertEqual(target.read_text(encoding='utf-8'), 'previous export')
            self.assertEqual(list(Path(directory).glob('*.part')), [])
