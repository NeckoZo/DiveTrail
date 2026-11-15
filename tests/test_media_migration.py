from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from matchlog.capture_journal import _review_retry_key
from matchlog.media_migration import CLEANUP_FLAG, migrate_media_layout
from matchlog.media_paths import existing_media_path, media_dir, resolve_media_path
from matchlog.storage import Store


AT = datetime(2026, 11, 7, 23, 25, 10, tzinfo=timezone(timedelta(hours=8)))


class MediaMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)
        self.db = self.store.db

    def file(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (1000000000, 1000000000))
        return path

    def add_match(self, ident, path, at=AT.timestamp()):
        data = {'id': ident, 'summary_captured_at': AT.isoformat(),
                'evidence': [{'kind': 'summary', 'path': str(path)}]}
        self.db.execute('INSERT INTO matches VALUES (?,?,?)', (ident, at, json.dumps(data)))
        self.db.execute('INSERT INTO evidence VALUES (?,?,?,?)', (ident, ident, 'summary', str(path)))

    def test_migrates_all_known_references_and_keeps_sample_names(self):
        summary_a = self.file('captures/a.webp', b'first match')
        summary_b = self.file('captures/b.webp', b'second match')
        hud = self.file('hud/old.webp', b'hud stats')
        squad = self.file('hud/old-squad.webp', b'hud teammates')
        job = self.file('inbox/old.png', b'recovery job')
        lobby = self.file('lobby/old.webp', b'lobby rank')
        profile = self.file('profiles/old.webp', b'profile stats')
        highlight = self.file('highlights/old.webp', b'highlight')
        sample = self.file('hero_samples/saved.png', b'hero portrait')
        index = self.file('hero_samples/index.json', b'{}')
        reference = self.file('reference_samples/group/summary.png', b'reference')
        unknown = self.file('reference_samples/notes.txt', b'retain')
        self.add_match('a', summary_a)
        self.add_match('b', summary_b)
        self.db.executemany('INSERT INTO hud_backups VALUES (?,?,?,?,?,?,?)', (
            ('hud', AT.timestamp(), 'S31', '1', json.dumps({'squad_path': str(squad)}), str(hud), 'a'),
            ('hud-legacy', AT.timestamp(), 'S31', '1', '{}', str(hud), 'b'),
        ))
        self.db.execute('INSERT INTO capture_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        ('job', 'round', '[]', AT.timestamp(), 'settlement', str(job), 'review', 0, None, None, None))
        self.db.execute('INSERT INTO recognition_retries VALUES (?,?)', (_review_retry_key(str(job), 'round'), 2))
        self.db.execute('INSERT INTO lobby_rank VALUES (?,?,?,?)', ('S31|1|user', AT.timestamp(), '{}', str(lobby)))
        self.db.execute('INSERT INTO lobby_history VALUES (?,?,?,?,?,?)',
                        ('lobby', '[]', AT.timestamp(), 'round', '{}', str(lobby)))
        self.db.execute('INSERT INTO profile_snapshots VALUES (?,?,?,?,?)',
                        ('profile', 'user', AT.timestamp(), '{}', str(profile)))
        self.db.execute('INSERT INTO match_highlights VALUES (?,?,?,?,?)',
                        ('a', str(highlight), 'hash', AT.timestamp(), '{}'))
        self.db.commit()

        report = migrate_media_layout(self.store)
        self.assertEqual((report['moved'], report['removed'], report['retained']), (11, 11, 0))
        backup = Path(report['backup'])
        self.assertTrue(backup.is_file())
        self.assertEqual(len(json.loads(backup.with_suffix('.media-map.json').read_text(encoding='utf-8'))), 11)
        targets = dict(self.db.execute('SELECT match_id,path FROM evidence'))
        self.assertEqual(Path(targets['a']).name, 'summary-261107-232510.webp')
        self.assertEqual(Path(targets['b']).name, 'summary-261107-232510-02.webp')
        self.assertEqual(Path(targets['a']).read_bytes(), b'first match')
        self.assertEqual(Path(targets['b']).read_bytes(), b'second match')
        self.assertEqual(Path(targets['a']).stat().st_mtime, 1000000000)
        data = json.loads(self.db.execute("SELECT payload FROM matches WHERE id='a'").fetchone()[0])
        self.assertEqual(data['evidence'][0]['path'], targets['a'])
        for raw, moved_hud in self.db.execute('SELECT payload,path FROM hud_backups'):
            self.assertEqual(Path(moved_hud).read_bytes(), b'hud stats')
            moved_squad = Path(json.loads(raw)['squad_path'])
            self.assertEqual(moved_squad.name, 'hud-squad-261107-232510.webp')
            self.assertEqual(moved_squad.read_bytes(), b'hud teammates')
        moved_job = self.db.execute('SELECT path FROM capture_jobs').fetchone()[0]
        self.assertEqual(Path(moved_job).name, 'queue-261107-232510.png')
        self.assertEqual(Path(moved_job).read_bytes(), b'recovery job')
        self.assertEqual(self.db.execute('SELECT * FROM recognition_retries').fetchall(),
                         [(_review_retry_key(moved_job, 'round'), 2)])
        for table, expected in (('lobby_rank', b'lobby rank'), ('lobby_history', b'lobby rank'),
                                ('profile_snapshots', b'profile stats'), ('match_highlights', b'highlight')):
            path = existing_media_path(self.root, self.db.execute(f'SELECT path FROM {table}').fetchone()[0])
            self.assertEqual(path.read_bytes(), expected)
        for old, key, relative in ((sample, 'hero_samples', 'saved.png'), (index, 'hero_samples', 'index.json'),
                                   (reference, 'reference_samples', 'group/summary.png')):
            self.assertFalse(old.exists())
            self.assertTrue((media_dir(self.root, key) / relative).is_file())
        self.assertEqual(unknown.read_bytes(), b'retain')
        self.assertEqual(migrate_media_layout(self.store)['moved'], 0)

    def test_failure_keeps_old_paths_and_reuses_verified_copy_on_retry(self):
        summary_a = self.file('captures/a.webp', b'first')
        summary_b = self.file('captures/b.webp', b'second')
        self.add_match('a', summary_a)
        self.add_match('b', summary_b)
        self.db.commit()
        from matchlog import media_migration
        original = media_migration._copy_verified

        def stop_on_second(source, target, digest):
            if source == summary_b:
                raise OSError('disk full')
            original(source, target, digest)

        with patch.object(media_migration, '_copy_verified', side_effect=stop_on_second):
            with self.assertRaisesRegex(OSError, 'disk full'):
                migrate_media_layout(self.store)
        self.assertTrue(summary_a.is_file())
        self.assertTrue(summary_b.is_file())
        self.assertEqual(self.db.execute('SELECT path FROM evidence ORDER BY match_id').fetchall(),
                         [(str(summary_a),), (str(summary_b),)])
        self.assertEqual(len(list(media_dir(self.root, 'summary').iterdir())), 1)
        self.assertEqual(migrate_media_layout(self.store)['removed'], 2)
        self.assertEqual(len(list(media_dir(self.root, 'summary').iterdir())), 2)

    def test_sample_collision_keeps_both_and_unmapped_paths_are_rejected(self):
        old = self.file('hero_samples/index.json', b'old index')
        old_sample = self.file('hero_samples/saved.png', b'old sample')
        current = self.file('sample/heroes/index.json', b'new index')
        self.assertEqual(migrate_media_layout(self.store)['retained'], 2)
        self.assertEqual(old.read_bytes(), b'old index')
        self.assertEqual(old_sample.read_bytes(), b'old sample')
        self.assertEqual(current.read_bytes(), b'new index')
        self.assertEqual(media_dir(Path(), 'inbox'), Path.cwd() / 'capture' / 'draft')
        self.assertEqual(resolve_media_path(self.root, self.root / 'inbox/old.png'), self.root / 'inbox/old.png')
        for relative in ('capture/top.png', 'capture/unknown/old.png', 'sample/old.png'):
            with self.assertRaises(ValueError):
                resolve_media_path(self.root, self.root / relative)

    def test_current_layout_migration_updates_cache_missing_refs_and_undo(self):
        draft = self.file('capture/inbox/queue-20261107-232509.png', b'full frame')
        rank = self.file('capture/settlement/incident-queue-20261107-232509.png', b'full frame')
        missing = self.root / 'capture/inbox/queue-20261107-232510.png'
        self.add_match('a', rank)
        before = self.db.execute("SELECT created,payload FROM matches WHERE id='a'").fetchone()
        undo = {'before': {'a': before}, 'after': {'a': before}}
        self.db.execute('INSERT INTO recorder_flags VALUES (?,?)', ('manual_merge_undo', json.dumps(undo)))
        self.db.execute("INSERT INTO recorder_flags VALUES ('media_layout_v1',1)")
        self.db.execute('INSERT INTO media_asset_paths VALUES (?,?,?)', ('queue', 'digest', str(draft)))
        self.db.execute('INSERT INTO capture_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        ('job', 'round', '[]', AT.timestamp(), 'settlement', str(missing), 'review', 0, None, None, None))
        self.db.execute('INSERT INTO recognition_retries VALUES (?,?)', (_review_retry_key(str(missing), 'round'), 3))
        self.db.commit()

        report = migrate_media_layout(self.store, incident_kinds={rank.name: 'rank_overview'})
        self.assertEqual((report['moved'], report['missing'], report['removed']), (2, 1, 2))
        moved_draft = self.root / 'capture/draft/queue-261107-232509.png'
        moved_rank = self.root / 'capture/rank/rank-overview-261107-232509-incident.png'
        self.assertEqual(moved_draft.read_bytes(), moved_rank.read_bytes())
        moved_draft.unlink()
        self.assertTrue(moved_rank.is_file())
        self.assertEqual(self.db.execute('SELECT path FROM media_asset_paths').fetchone(), (str(moved_draft),))
        moved_missing = self.root / 'capture/draft/queue-261107-232510.png'
        self.assertFalse(moved_missing.exists())
        self.assertEqual(self.db.execute('SELECT path FROM capture_jobs').fetchone(), (str(moved_missing),))
        self.assertEqual(self.db.execute('SELECT * FROM recognition_retries').fetchall(),
                         [(_review_retry_key(str(moved_missing), 'round'), 3)])
        raw = self.db.execute("SELECT value FROM recorder_flags WHERE name='manual_merge_undo'").fetchone()[0]
        match = self.db.execute("SELECT created,payload FROM matches WHERE id='a'").fetchone()
        self.assertEqual(json.loads(raw)['after']['a'], list(match))
        self.assertEqual(json.loads(match[1])['evidence'][0]['path'], str(moved_rank))

    def test_interrupted_cleanup_resumes_after_reference_commit(self):
        path = self.file('capture/settlement/rank-20261107-232510.webp', b'original frame')
        from matchlog import media_migration
        original = media_migration._finish_cleanup

        def stop_after_commit(db, root, report):
            if db.execute('SELECT 1 FROM recorder_flags WHERE name=?', (CLEANUP_FLAG,)).fetchone():
                raise OSError('stopped after commit')
            return original(db, root, report)

        with patch.object(media_migration, '_finish_cleanup', side_effect=stop_after_commit):
            with self.assertRaisesRegex(OSError, 'stopped after commit'):
                migrate_media_layout(self.store)
        target = self.root / 'capture/rank/rank-261107-232510.webp'
        self.assertEqual(path.read_bytes(), target.read_bytes())
        self.assertEqual(migrate_media_layout(self.store)['removed'], 1)
        self.assertFalse(path.exists())
        self.assertTrue(target.is_file())
