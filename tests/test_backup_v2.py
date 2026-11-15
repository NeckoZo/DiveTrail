"""Portable restore of the data needed on a second computer."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from PIL import Image

from matchlog.heroes import HeroSamples
from matchlog.media_paths import media_dir
from matchlog.season_calendar import SeasonCalendar
from matchlog.storage import Store
from matchlog.store_backup import export_backup, import_backup


class BackupV2(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.store('source', 'PlayerOne')
        self.target = self.store('target')
        self.archive = self.root / 'portable.matchlog'

    def store(self, folder, name=None):
        store = Store(self.root / folder)
        self.addCleanup(store.db.close)
        if name:
            store.configure({'self_name': name})
        return store

    def portrait(self, store, label='万蒂奇'):
        samples = HeroSamples(store.root / 'sample' / 'heroes')
        image = Image.new('RGB', (64, 64))
        image.putdata([(x * 4, y * 4, ((x + y) % 64) * 4) for y in range(64) for x in range(64)])
        ident = hashlib.sha256(image.tobytes()).hexdigest()[:20]
        samples.folder.mkdir(parents=True,exist_ok=True)
        image.save(samples.folder / (ident + '.png'))
        samples._write({ident: {'hero': None, 'created_at': '2026-11-01T00:00:00+00:00'}})
        if label is not None:
            self.assertTrue(samples.confirm(ident, label))
        return ident

    def capture(self, color, status, round_id=None, timestamp=1000):
        journal = self.source.journal
        ident = journal.enqueue(Image.new('RGB', (30, 30), color), 'settlement',
                                round_id=round_id, timestamp=timestamp)
        self.source.db.execute('UPDATE capture_jobs SET status=? WHERE id=?', (status, ident))
        self.source.db.commit()
        return ident

    def rewrite(self, callback):
        with zipfile.ZipFile(self.archive) as source:
            entries = {name: source.read(name) for name in source.namelist()}
        callback(entries)
        with zipfile.ZipFile(self.archive, 'w') as target:
            for name, data in entries.items():
                target.writestr(name, data)

    def test_v2_migrates_confirmed_portraits_calendar_and_eligible_jobs(self):
        ident = self.portrait(self.source)
        SeasonCalendar(self.source.root).add_local('S32', '2', '2027-01-10T09:00')
        rid = self.source.journal.note_screen('settlement', timestamp=900)
        running = self.capture('red', 'processing', rid)
        review = self.capture('blue', 'review', rid, timestamp=1010)
        self.capture('white', 'done', rid)
        vanished = self.capture('green', 'failed', rid)
        Path(self.source.journal.get(vanished)['path']).unlink()
        result = export_backup(self.source, self.archive)
        self.assertEqual((result['capture_jobs'], result['portrait_samples'], result['schedule']), (2, 1, True))
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(json.loads(archive.read('manifest.json'))['version'], 2)
            self.assertNotIn(str(self.source.root).encode(), archive.read('records.json'))
        result = import_backup(self.target, self.archive)
        self.assertEqual((result['capture_jobs'], result['portrait_samples'], result['schedule']), (2, 1, True))
        self.assertFalse(result['schedule_preserved'])
        self.assertEqual(self.target.journal.get(running)['status'], 'pending')
        self.assertEqual(self.target.journal.get(review)['status'], 'review')
        self.assertEqual({row['id'] for row in self.target.journal.public()}, {running, review})
        self.assertEqual(self.target.journal.get(review)['created'], 1010)
        self.assertTrue(Path(self.target.journal.get(review)['path']).is_file())
        self.assertEqual(Path(self.target.journal.get(review)['path']).parent, media_dir(self.target.root, 'draft'))
        self.assertEqual(SeasonCalendar(self.target.root).data['transitions'][0]['at'], '2027-01-10T01:00:00+00:00')
        samples = HeroSamples(self.target.root / 'sample' / 'heroes')
        self.assertEqual(samples.entries()[ident]['hero'], '万蒂奇')
        self.assertEqual(len(samples.entries()[ident]['signature']), 256)
        self.assertTrue((samples.folder / (ident + '.png')).is_file())
        result = import_backup(self.target, self.archive)
        self.assertEqual((result['capture_jobs'], result['portrait_samples'], result['schedule']), (0, 0, False))
        self.assertTrue(result['schedule_preserved'])

    def test_hud_squad_evidence_round_trips_without_original_absolute_path(self):
        hud = media_dir(self.source.root, 'hud')
        hud.mkdir(parents=True)
        hud_path, squad_path = (hud / name for name in ('hud.png', 'hud-squad.png'))
        Image.new('RGB', (8, 8), 'red').save(hud_path)
        Image.new('RGB', (8, 8), 'blue').save(squad_path)
        self.source.db.execute('INSERT INTO hud_backups VALUES (?,?,?,?,?,?,?)',
                               ('hud', 1000, 'S31', '1', json.dumps({'squad_path': str(squad_path)}), str(hud_path), None))
        self.source.db.commit()

        export_backup(self.source, self.archive)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertNotIn(str(self.source.root).encode(), archive.read('records.json'))
        import_backup(self.target, self.archive)
        path, payload = self.target.db.execute('SELECT path,payload FROM hud_backups').fetchone()
        self.assertEqual(Path(path).read_bytes(), hud_path.read_bytes())
        restored = Path(json.loads(payload)['squad_path'])
        self.assertEqual(restored.parent, media_dir(self.target.root, 'restored'))
        self.assertEqual(restored.read_bytes(), squad_path.read_bytes())

    def test_conflicting_local_schedule_portrait_and_round_are_preserved(self):
        ident = self.portrait(self.source, '万蒂奇')
        self.assertEqual(self.portrait(self.target, '恶灵'), ident)
        local_calendar = SeasonCalendar(self.target.root)
        local_calendar.add_local('S32', '1', '2027-02-10T09:00')
        SeasonCalendar(self.source.root).add_local('S32', '2', '2027-01-10T09:00')
        round_id = self.source.journal.note_screen('settlement', timestamp=900)
        self.capture('red', 'pending', round_id)
        self.target.configure({'self_name': 'OtherPlayer'})
        self.target.db.execute("INSERT INTO capture_rounds (id,scope,started,seen,phase) VALUES (?,?,?,?,?)",
                               (round_id, self.target.journal.scope(), 900, 900, 'settlement'))
        self.target.db.commit()
        export_backup(self.source, self.archive)
        result = import_backup(self.target, self.archive)
        self.assertEqual((result['capture_jobs'], result['portrait_samples'], result['schedule']), (0, 0, False))
        self.assertEqual((result['capture_jobs_skipped'], result['portrait_samples_skipped'], result['schedule_preserved']),
                         (1, 1, True))
        self.assertEqual(self.target.settings()['self_name'], 'OtherPlayer')
        self.assertEqual(SeasonCalendar(self.target.root).data, local_calendar.data)
        self.assertEqual(HeroSamples(self.target.root / 'sample' / 'heroes').entries()[ident]['hero'], '恶灵')
        self.assertFalse(media_dir(self.target.root, 'draft').exists())

    def test_queue_cleanup_never_removes_persisted_evidence_with_identical_bytes(self):
        round_id = self.source.journal.note_screen('settlement', timestamp=900)
        job = self.capture('red', 'pending', round_id)
        path = self.source.journal.get(job)['path']
        match_id = self.source.ingest({'kind': 'summary', 'placement': 5,
                                       'players': [{'name': 'PlayerOne', 'damage': 100}]},
                                      path, round_id=round_id)
        export_backup(self.source, self.archive)
        self.assertEqual(import_backup(self.target, self.archive)['capture_jobs'], 1)
        record, = [match for match in self.target.all() if match['id'] == match_id]
        evidence = Path(record['evidence'][0]['path'])
        capture = Path(self.target.journal.get(job)['path'])
        self.assertNotEqual(evidence, capture)
        self.assertEqual(evidence.read_bytes(), capture.read_bytes())
        self.target.journal.finish(job, 'done')
        self.assertFalse(capture.exists())
        self.assertTrue(evidence.exists())

    def test_invalid_confirmed_portrait_rolls_back_queue_and_schedule(self):
        ident = self.portrait(self.source)
        SeasonCalendar(self.source.root).add_local('S32', '2', '2027-01-10T09:00')
        self.capture('red', 'pending')
        export_backup(self.source, self.archive)

        def invalid(entries):
            data = json.loads(entries['extras.json'])
            data['hero_samples']['a' * 20] = data['hero_samples'].pop(ident)
            entries['extras.json'] = json.dumps(data)

        self.rewrite(invalid)
        with self.assertRaisesRegex(ValueError, '校验失败'):
            import_backup(self.target, self.archive)
        self.assertFalse((self.target.root / 'season_schedule.json').exists())
        self.assertFalse(media_dir(self.target.root, 'draft').exists())
        self.assertEqual(self.target.db.execute('SELECT COUNT(*) FROM capture_jobs').fetchone()[0], 0)
        self.assertEqual(self.target.settings()['self_name'], '')

    def test_invalid_queue_media_reference_is_rejected(self):
        self.capture('red', 'pending')
        export_backup(self.source, self.archive)

        def invalid(entries):
            data = json.loads(entries['records.json'])
            data['capture_jobs'][0]['path'] = '@missing/../../local.png'
            entries['records.json'] = json.dumps(data)

        self.rewrite(invalid)
        with self.assertRaisesRegex(ValueError, '图片路径'):
            import_backup(self.target, self.archive)
        self.assertFalse(media_dir(self.target.root, 'draft').exists())

    def test_unconfirmed_portraits_do_not_transfer(self):
        self.portrait(self.source, label=None)
        self.assertEqual(export_backup(self.source, self.archive)['portrait_samples'], 0)
        self.assertEqual(import_backup(self.target, self.archive)['portrait_samples'], 0)
        self.assertFalse((self.target.root / 'sample' / 'heroes').exists())
