"""A verified nickname change keeps previous evidence in the right archive."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from matchlog.match_audit import recover_match, recover_profile_fields
from matchlog.media_paths import media_dir
from matchlog.migrations import backup_database
from matchlog.profile_rename import relabel_historical_result, rename_profile
from matchlog.storage import Store


class ProfileRenameTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.addCleanup(self.store.db.close)
        self.store.configure({'self_name': 'OldName'})
        self.at = time.time() - 30

    def seed(self, own='OldName', teammate='Friend'):
        summary = {'kind': 'summary', 'placement': 3, 'players': [
            {'name': own, 'kills': 2}, {'name': teammate, 'kills': 1},
        ]}
        round_id = self.store.journal.note_screen('settlement', self.at)
        match_id = self.store.ingest(summary, self.store.root/'summary.webp', digest=own,
                                     timestamp=self.at, round_id=round_id)
        return round_id, match_id, summary

    def test_merge_migrates_scopes_and_preserves_backup_evidence_and_rank(self):
        round_id, match_id, summary = self.seed()
        store = self.store
        with Image.new('RGB', (8, 8)) as image:
            job_id = store.journal.enqueue(image, 'settlement', round_id, timestamp=self.at)
        old_job = store.journal.get(job_id)
        store.journal.observe(job_id, summary, json.dumps(summary, sort_keys=True, ensure_ascii=False))
        store.save_hud_backup({'round_id': round_id, 'players': [{'name': 'OldName', 'slot': 2}]},
                              store.root/'hud.webp', timestamp=self.at)
        profile = {'name': 'OldName', 'lifetime': {'wins': 12}, 'season': {'matches': 9}}
        store.save_profile(profile, store.root/'profile.webp', timestamp=self.at)
        lobby = store.save_lobby_rank({'total_rp': 1000}, store.root/'lobby.webp', timestamp=self.at)
        store.journal.save_lobby(lobby, store.root/'lobby.webp', round_id=round_id, timestamp=self.at)
        store.configure({'initial_rp': 950})
        store.calibrate_rp(1000)

        report = rename_profile(store, '[TEAM] NewName', merge=True, expected_name='OldName')
        self.assertEqual(report['matches'], 1)
        self.assertEqual(report['config']['initial_rp'], 950)
        with closing(sqlite3.connect(report['backup'])) as copy:
            payload = copy.execute('SELECT payload FROM matches WHERE id=?', (match_id,)).fetchone()[0]
            self.assertEqual(json.loads(payload)['account'], 'oldname')
        match = store.all()[0]
        self.assertEqual((match['id'], match['created'], match['summary']['placement']), (match_id, self.at, 3))
        self.assertEqual(match['evidence'][0]['path'], str(store.root/'summary.webp'))
        self.assertEqual([p['name'] for p in match['summary']['players']], ['[TEAM] NewName', 'Friend'])
        self.assertEqual(store.hud_backups()[0]['players'][0]['name'], '[TEAM] NewName')
        self.assertEqual(store.latest_profile()['name'], '[TEAM] NewName')
        self.assertEqual(store.latest_lobby_rank()['total_rp'], 1000)
        self.assertEqual(store.rank_progress()['total_rp'], 1000)
        job = store.journal.get(job_id)
        self.assertEqual(job['scope'], store.journal.scope())
        self.assertEqual(job['path'], old_job['path'])
        self.assertEqual(job['payload']['players'][0]['name'], '[TEAM] NewName')
        self.assertEqual(job['signature'], json.dumps(job['payload'], sort_keys=True, ensure_ascii=False))
        parsed = relabel_historical_result(store, summary, round_id, self.at)
        self.assertEqual(parsed['players'][0]['name'], '[TEAM] NewName')
        self.assertEqual(summary['players'][0]['name'], 'OldName')

    def test_new_archive_preserves_old_and_can_reopen_it(self):
        _, match_id, _ = self.seed()
        self.store.configure({'initial_rp': 950})
        report = rename_profile(self.store, 'OtherName')
        self.assertIsNone(report['backup'])
        self.assertEqual(self.store.all(), [])
        self.assertIsNone(self.store.rank_progress()['total_rp'])
        rename_profile(self.store, 'OldName', expected_name='OtherName')
        self.assertEqual(self.store.all()[0]['id'], match_id)
        self.assertEqual(self.store.rank_progress()['total_rp'], 950)

    def test_merge_with_occupied_target_or_stale_confirmation_keeps_both_archives(self):
        _, first, _ = self.seed()
        rename_profile(self.store, 'OtherName')
        _, other, _ = self.seed(own='OtherName')
        rename_profile(self.store, 'OldName')
        with self.assertRaisesRegex(ValueError, '建立新档案'):
            rename_profile(self.store, 'OtherName', merge=True, expected_name='OldName')
        self.assertEqual(self.store.all()[0]['id'], first)
        with self.assertRaisesRegex(ValueError, '重新确认'):
            rename_profile(self.store, 'AnotherName', merge=True, expected_name='OtherName')
        rename_profile(self.store, 'OtherName')
        self.assertEqual(self.store.all()[0]['id'], other)

    def test_roster_name_collision_rolls_back(self):
        round_id, match_id, _ = self.seed(teammate='NewName')
        old_scope = self.store.journal.scope()
        with self.assertRaisesRegex(ValueError, '建立新档案'):
            rename_profile(self.store, 'NewName', merge=True)
        self.assertEqual(self.store.all()[0]['id'], match_id)
        self.assertEqual(self.store.journal.scope(), old_scope)
        scope = self.store.db.execute('SELECT scope FROM capture_rounds WHERE id=?', (round_id,)).fetchone()[0]
        self.assertEqual(scope, old_scope)

    def test_merge_aborts_if_another_instance_writes_after_backup(self):
        _, match_id, _ = self.seed()
        store = self.store

        def concurrent_write(source, label):
            backup = backup_database(source, label)
            second = Store(store.root)
            try:
                second.configure({'theme': 'light'})
            finally:
                second.db.close()
            return backup

        with patch('matchlog.profile_rename.backup_database', side_effect=concurrent_write):
            with self.assertRaisesRegex(ValueError, '另一窗口'):
                rename_profile(store, 'NewName', merge=True)
        self.assertEqual(store.all()[0]['id'], match_id)
        self.assertEqual(store.settings()['theme'], 'light')

    def test_previous_name_never_authorizes_live_new_round_or_teammate_hud(self):
        round_id, _, summary = self.seed()
        rename_profile(self.store, 'NewName', merge=True)
        future = time.time() + 2
        new_round = self.store.journal.note_screen('hud', future)
        self.assertNotEqual(new_round, round_id)
        self.assertIs(relabel_historical_result(self.store, summary, new_round, future), summary)
        self.assertIs(relabel_historical_result(self.store, summary, round_id, future), summary)
        hud = {'kind': 'hud', 'players': [{'name': 'OldName', 'slot': 0}, {'name': 'Spectated', 'slot': 2}]}
        self.assertIs(relabel_historical_result(self.store, hud, round_id, self.at), hud)
        hud['players'][0]['slot'] = 2
        self.assertEqual(relabel_historical_result(self.store, hud, round_id, self.at)['players'][0]['name'],
                         'NewName')

    def test_archived_summary_and_profile_can_be_recovered_after_rename(self):
        store = self.store
        round_id, match_id, summary = self.seed()
        full = {**summary, 'players': [
            {**player, 'assists': 1, 'knocks': 3, 'damage': 830}
            for player in [*summary['players'], {'name': 'AnotherFriend', 'kills': 1}]
        ]}
        old_profile = {'kind': 'profile', 'name': 'OldName', 'season': {'matches': 9}}
        profile_dir = media_dir(store.root, 'profiles')
        profile_dir.mkdir(parents=True)
        profile_path = profile_dir/'profile-20261108-110000.webp'
        with Image.new('RGB', (8, 8)) as image:
            image.save(profile_path)
            store.journal.enqueue(image, 'settlement', round_id, timestamp=self.at)
        store.save_profile(old_profile, profile_path, timestamp=self.at)
        rename_profile(store, 'NewName', merge=True)
        ocr = Mock()
        ocr.read.side_effect = lambda path: (
            {**old_profile, 'season': {'matches': 9, 'kills': 11}} if path == profile_path else full
        )
        self.assertEqual(recover_match(store, ocr, match_id), 1)
        self.assertEqual(store.all()[0]['summary']['players'][0]['damage'], 830)
        self.assertEqual(recover_profile_fields(store, ocr), 1)
        self.assertEqual(store.latest_profile()['season']['kills'], 11)

    def test_old_name_can_fill_a_migrated_manual_match_but_not_a_new_upload(self):
        store = self.store
        manual = store.create_manual({'mode': 'ranked', 'time': datetime.fromtimestamp(self.at, timezone.utc).isoformat()})
        round_id = manual.pop('round_id')
        with store.db:
            store.db.execute('DELETE FROM capture_rounds WHERE id=?', (round_id,))
            store._save(manual, commit=False)
        _, _, summary = self.seed()
        rename_profile(store, 'NewName', merge=True)
        target, at = store.journal.upload_round(manual['id'])
        self.assertEqual(relabel_historical_result(store, summary, target, at)['players'][0]['name'],
                         'NewName')
        unknown, at = store.journal.upload_round()
        self.assertIs(relabel_historical_result(store, summary, unknown, at), summary)
