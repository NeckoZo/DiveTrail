from contextlib import closing
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from matchlog.capture_diagnostics import CaptureDiagnostics
from matchlog.community import Community, RPC, anonymous_rows
from matchlog.hud_score import hud_score_evidence
from matchlog.storage import Store


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.time = 1794270000
        self.calls = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = Community(self.temp.name, send=self.send, clock=lambda: self.time)

    def send(self, path, body, headers):
        self.calls.append((path, body, headers))
        if path.endswith('/signin/anonymously'):
            return {'access_token': 'synthetic', 'expires_in': 3600, 'refresh_token': 'refresh'}
        if path == RPC + 'matchlog_submit_v1':
            self.assertEqual(headers, {'Authorization': 'Bearer synthetic'})
            return {'accepted': len(body['matches'])}
        if path == RPC + 'matchlog_summary_v1':
            return {'ready': False}
        raise AssertionError(path)

    def row(self, **fields):
        return dict(id='local-private-id', created=self.time - 4000, season='S31',
                    split=1, mode='ranked', evidence=['private.png'], game_key='private-key',
                    summary={'placement': 2, 'players': [
                        {'name': 'synthetic-player', 'hero': 'Vantage', 'damage': 789},
                        {'name': 'synthetic-teammate', 'hero': 'Pathfinder', 'damage': 321},
                    ]}, **fields)

    def test_opt_in_and_allowlist_with_explicit_confirmation(self):
        owner = 'synthetic-player'
        self.assertFalse(self.service.status()['enabled'])
        with self.assertRaises(ValueError):
            self.service.preview([self.row()], owner)
        self.service.set_enabled(True)
        self.assertEqual(self.service.preview([self.row()], owner)['count'], 0)
        self.assertEqual(self.calls, [])
        self.time += 8000
        row = self.row()
        preview = self.service.preview([row], owner)
        self.assertEqual(preview['count'], 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.service.upload(preview['token'], owner, [row]), {'count': 1})
        [(path, body, _)] = [call for call in self.calls if call[0].endswith('submit_v1')]
        [sent] = body['matches']
        self.assertEqual(path, RPC + 'matchlog_submit_v1')
        self.assertEqual(set(sent), {'key','day','season','split','mode','hero','placement','damage'})
        self.assertRegex(sent['key'], r'^[a-f0-9]{64}$')
        self.assertEqual((sent['hero'], sent['damage']), ('vantage', 789))
        self.assertEqual(self.service.preview([row], owner)['count'], 0)
        self.assertEqual(self.service.aggregate('S31', '1', 'ranked'), {'ready': False})
        self.service.set_enabled(False)
        with self.assertRaises(ValueError):
            self.service.upload(preview['token'], owner, [row])

    def test_preview_rejects_change_and_expiration(self):
        self.service.set_enabled(True)
        self.time += 8000
        owner = 'synthetic-player'
        row = self.row()
        preview = self.service.preview([row], owner)
        row['summary']['placement'] = 3
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.service.upload(preview['token'], owner, [row])
        token = self.service.preview([row], owner)['token']
        self.time += 301
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.service.upload(token, owner, [row])
        self.assertEqual(self.calls, [])

    def test_uploads_and_unsettled_records_are_excluded(self):
        for row in (self.row(record_origin='upload'), self.row(record_source='opening'),
                    self.row(record_source='last_snapshot'), self.row(deleted_at=self.time)):
            self.assertEqual(anonymous_rows([row], 'synthetic-player', 'local', 0, self.time), [])

    def test_unavailable_service_has_no_local_receipt(self):
        def unavailable(path, body, headers):
            if path.endswith('submit_v1'):
                raise HTTPError('unused', 404, 'missing function', {}, None)
            return self.send(path, body, headers)
        self.service = Community(self.temp.name, send=unavailable, clock=lambda: self.time)
        self.service.set_enabled(True)
        self.time += 8000
        row, owner = self.row(), 'synthetic-player'
        token = self.service.preview([row], owner)['token']
        with self.assertRaisesRegex(ValueError, '未部署'):
            self.service.upload(token, owner, [row])
        self.assertIsNone(self.service.status()['last_upload'])
        self.assertEqual(self.service.preview([row], owner)['count'], 1)

    def test_diagnostics_preview_filters_retained_events_and_retries_batches(self):
        folder = Path(self.temp.name) / 'logs'
        folder.mkdir()
        events = [(self.time - 90000, 'seen', 'lobby', None, 1)] + [
            (self.time - index - 10, 'reading', 'rank', index + 1, 2) for index in range(501)
        ]
        invalid = (self.time - 1, 'private-text', 'rank', None, None)
        path = folder / 'capture-261110.jsonl.old'
        path.write_text('\n'.join(json.dumps(row) for row in [*events, invalid]), encoding='utf-8')
        trail = CaptureDiagnostics(limit=2, folder=folder)
        preview = self.service.preview_diagnostics(trail, '24h', '0.19.0')
        self.assertEqual((preview['count'], preview['shown']), (501, 400))
        all_logs = self.service.preview_diagnostics(trail, 'all', '0.19.0')
        self.assertEqual(all_logs['count'], 502)
        self.assertEqual(self.calls, [])

        def submit(path, body, headers):
            if not path.endswith('diagnostics_v1'):
                return self.send(path, body, headers)
            self.calls.append((path, body, headers))
            if body['batch']['index'] == 1 and len(self.calls) == 3:
                return {'accepted': 0}
            return {'accepted': len(body['batch']['events'])}

        with patch.object(self.service, '_send', submit):
            with self.assertRaisesRegex(ValueError, '失效'):
                self.service.upload_diagnostics(preview['token'])
            with self.assertRaisesRegex(ValueError, '重试'):
                self.service.upload_diagnostics(all_logs['token'])
            result = self.service.upload_diagnostics(all_logs['token'])
        self.assertEqual(result['count'], 502)
        self.assertFalse(self.service.status()['enabled'])
        self.assertRegex(result['receipt'], r'^[a-f0-9]{16}$')
        batches = [body['batch'] for path, body, _ in self.calls if path.endswith('diagnostics_v1')]
        self.assertEqual([len(batch['events']) for batch in batches], [500, 2, 500, 2])
        self.assertEqual(batches[:2], batches[2:])
        self.assertEqual(set(batches[0]), {'receipt', 'range', 'version', 'index', 'total', 'events'})
        self.assertNotIn('private-text', json.dumps(batches))

    def test_feedback_only_is_previewed_locally_and_uploaded_on_confirmation(self):
        class NoLogsExpected:
            def retained_events(self, _since, _now):
                raise AssertionError('Text-only feedback should never read logs')

        def submit(path, body, headers):
            if path != RPC + 'matchlog_submit_diagnostics_v2':
                return self.send(path, body, headers)
            self.calls.append((path, body, headers))
            return {'accepted': 0}

        trail = NoLogsExpected()
        self.assertIsNone(self.service.preview_diagnostics(trail, 'none', '1.0.0', '  ')['token'])
        with self.assertRaisesRegex(ValueError, '1000'):
            self.service.preview_diagnostics(trail, 'none', '1.0.0', '字' * 1001)
        with self.assertRaisesRegex(ValueError, '1000'):
            self.service.preview_diagnostics(trail, 'none', '1.0.0', 'abc\x00def')
        note = '建议增加查询\n附加说明'
        draft = self.service.preview_diagnostics(trail, 'none', '1.0.0', '  建议增加查询\r\n附加说明  ')
        self.assertEqual((draft['count'], draft['feedback']), (0, note))
        self.assertFalse(self.calls)
        with patch.object(self.service, '_send', submit):
            result = self.service.upload_diagnostics(draft['token'])
        self.assertEqual(result['count'], 0)
        self.assertEqual(self.calls[-1][1]['batch'], {
            'receipt': self.calls[-1][1]['batch']['receipt'], 'range': 'none', 'version': '1.0.0',
            'index': 0, 'total': 1, 'events': [], 'feedback': note,
        })

    def test_feedback_attaches_to_first_batch_and_preserves_v1_for_remaining_logs(self):
        class Trail:
            def retained_events(inner, _since, _now):
                return [(self.time - 1, 'reading', 'rank', i + 1, 1) for i in range(501)]

        def submit(path, body, headers):
            if 'diagnostics' not in path:
                return self.send(path, body, headers)
            self.calls.append((path, body, headers))
            return {'accepted': len(body['batch']['events'])}

        with patch.object(self.service, '_send', submit):
            draft = self.service.preview_diagnostics(Trail(), 'all', '1.0.0', '结算页读取慢')
            result = self.service.upload_diagnostics(draft['token'])
        self.assertEqual(result['count'], 501)
        first, second = self.calls[1:]
        self.assertEqual(first[0], RPC + 'matchlog_submit_diagnostics_v2')
        self.assertEqual(first[1]['batch']['feedback'], '结算页读取慢')
        self.assertEqual(second[0], RPC + 'matchlog_submit_diagnostics_v1')
        self.assertNotIn('feedback', second[1]['batch'])
        self.assertEqual(first[1]['batch']['receipt'], second[1]['batch']['receipt'])


class HudScoreTests(unittest.TestCase):
    def test_summary_keeps_only_same_round_personal_score(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name))
        with closing(store.db):
            store.configure({'self_name':'synthetic-player', 'season':'S31', 'split':'1'})
            config = store.settings()
            journal = store.journal
            key = '4:1000001:1000002:1ab0cd12'
            round_id, _ = journal.upload_round(game_key=key)
            store.db.execute('UPDATE capture_rounds SET match_id=? WHERE id=?', ('local', round_id))
            for value, at, identity, game_key in (
                (182, 115, 'self', key), (228, 118, 'spectator', key), (-331, 119, 'self', key),
                (250, 120, 'self', '4:1000001:1000002:1ab0cd13'), (300, 150, 'self', key),
            ):
                store.save_hud_backup({'round_id':round_id, 'identity':identity,
                                       'game_key':game_key, 'live_rp':value}, Path(tmp.name) / 'hud.webp', at)
            settled = {'id':'local', 'mode':'ranked', 'round_end_at':120,
                       'entry_cost_at_start':38, 'rank':{'delta':240}}
            self.assertEqual(hud_score_evidence(store, [settled], config), {'local':{'at':115, 'value':182}})
            self.assertEqual(settled['rank']['delta'], 240)
