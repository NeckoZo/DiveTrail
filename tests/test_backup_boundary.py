import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from matchlog.media_paths import existing_media_path, resolve_media_path
from matchlog.storage import Store
from matchlog.store_backup import import_backup


def picture(color):
    output = BytesIO()
    Image.new('RGB', (10, 10), color).save(output, 'PNG')
    data = output.getvalue()
    return f'media/{hashlib.sha256(data).hexdigest()}.png', data


def match(path, ident='match-1', account='nz'):
    return {'id': ident, 'created': 100, 'payload': {
        'id': ident, 'created': 100, 'time': '1970-01-01T08:01:40+08:00',
        'season': 'S31', 'split': '1', 'account': account,
        'summary': {'players': [{'name': 'NZ', 'kills': 0}]},
        'rank': None, 'evidence': [{'kind': 'summary', 'path': path}],
    }}


def archive(data, media=()):
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as backup:
        backup.writestr('manifest.json', '{"format":"MatchLog","version":1}')
        backup.writestr('records.json', json.dumps(data))
        for name, content in media:
            backup.writestr(name, content)
    output.seek(0)
    return output


class BackupBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'data')
        self.store.configure({'self_name': 'NZ'})
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.db.close)

    def test_bare_relative_and_traversal_references_are_rejected(self):
        for path in ('../outside.png', 'captures/local.png', '@missing/../outside.png',
                     r'C:\outside.png', '@media/000.png'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                import_backup(self.store, archive({'matches': [match(path)]}))
        self.assertEqual(self.store.all(), [])
        self.assertFalse((self.store.root / 'capture' / 'restored').exists())

    def test_nested_structure_is_rejected_before_images_are_written(self):
        name, content = picture('blue')
        record = match('@' + name)
        record['payload']['summary'] = {'players': [{'name': 'NZ', 'kills': 'twelve'}]}
        with self.assertRaisesRegex(ValueError, '字段'):
            import_backup(self.store, archive({'matches': [record]}, [(name, content)]))
        self.assertFalse((self.store.root / 'capture' / 'restored').exists())
        self.assertEqual(self.store.all(), [])

    def test_legacy_missing_markers_are_kept_unavailable(self):
        old = archive({'matches': [match('@missing/removed.webp'), match('', 'match-2')]})
        result = import_backup(self.store, old)
        self.assertEqual(result['imported'], 2)
        for row in self.store.all():
            path = resolve_media_path(self.store.root, row['evidence'][0]['path'])
            self.assertEqual(path.parent, self.store.root / 'capture' / 'restored')
            self.assertIsNone(existing_media_path(self.store.root, path))

    def test_manifest_and_media_have_independent_expansion_bounds(self):
        name, content = picture('green')
        zipped = archive({'matches': [match('@' + name)]}, [(name, content)])
        for constant in ('MAX_MANIFEST_BYTES', 'MAX_RECORDS_BYTES', 'MAX_IMAGE_BYTES'):
            with self.subTest(constant=constant), patch('matchlog.store_backup.' + constant, 10):
                zipped.seek(0)
                with self.assertRaisesRegex(ValueError, '条目过大'):
                    import_backup(self.store, zipped)
        self.assertEqual(self.store.all(), [])

    def test_individual_record_is_bounded_before_restore(self):
        record = match('')
        record['payload']['unused'] = 'x' * 10_000
        with patch('matchlog.store_backup.MAX_ROW_PAYLOAD_BYTES', 2000):
            with self.assertRaisesRegex(ValueError, '单条记录过大'):
                import_backup(self.store, archive({'matches': [record]}))
        self.assertEqual(self.store.all(), [])

    def test_referenced_media_requires_a_valid_image_and_digest(self):
        name, content = picture('green')
        fake = b'text in a png filename'
        fake_name = f'media/{hashlib.sha256(fake).hexdigest()}.png'
        for ref, data in ((name, content + b'changed'), (fake_name, fake)):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                import_backup(self.store, archive({'matches': [match('@' + ref)]}, [(ref, data)]))
        self.assertEqual(self.store.all(), [])
        self.assertFalse((self.store.root / 'capture' / 'restored').exists())

    def test_restore_conflict_rolls_back_records_and_new_images(self):
        media = sorted([picture('red'), picture('blue')])
        restored = self.store.root / 'capture' / 'restored'
        restored.mkdir(parents=True)
        first = restored / Path(media[0][0]).name
        conflicting = restored / Path(media[1][0]).name
        conflicting.write_bytes(b'existing data')
        records = {'matches': [match('@' + name, f'match-{i}') for i, (name, _) in enumerate(media)]}
        with self.assertRaisesRegex(ValueError, '校验值'):
            import_backup(self.store, archive(records, media))
        self.assertEqual(conflicting.read_bytes(), b'existing data')
        self.assertFalse(first.exists())
        self.assertEqual(self.store.all(), [])
        self.assertEqual(list(self.store.root.glob('.restore-*')), [])

    def test_v1_media_restore_is_idempotent_and_scoped(self):
        name, content = picture('green')
        unused = picture('red')
        backup = archive({'matches': [match('@' + name)]}, [(name, content), unused])
        self.assertEqual(import_backup(self.store, backup), {'imported': 1, 'skipped': 0, 'images': 1})
        row = self.store.all()[0]
        self.assertEqual(existing_media_path(self.store.root, row['evidence'][0]['path']).read_bytes(), content)
        self.assertFalse((self.store.root / 'capture' / 'restored' / Path(unused[0]).name).exists())
        backup.seek(0)
        self.assertEqual(import_backup(self.store, backup), {'imported': 0, 'skipped': 1, 'images': 1})

    def test_v1_owner_backfill_does_not_claim_existing_records(self):
        existing = match('', 'existing', account=None)
        self.store.db.execute('INSERT INTO matches VALUES (?,?,?)',
                              (existing['id'], existing['created'], json.dumps(existing['payload'])))
        self.store.db.commit()
        rounds = [
            {'id': 'round-1', 'scope': ['S31', '1', 'nz'], 'started': 100,
             'seen': 101, 'phase': 'settlement', 'match_id': 'match-1'},
            {'id': 'round-2', 'scope': ['S31', '1', 'nz'], 'started': 100,
             'seen': 101, 'phase': 'settlement', 'match_id': 'existing'},
        ]
        source = archive({'matches': [match('', account=None)], 'capture_rounds': rounds})
        self.assertEqual(import_backup(self.store, source)['imported'], 1)
        self.assertEqual([row['id'] for row in self.store.all()], ['match-1'])
        self.assertEqual(self.store.profile_isolation()['unassigned'], 1)

    def test_media_path_resolver_rejects_outside_files_and_non_image_data(self):
        root = self.store.root
        for path in ('captures/image.png', root / 'captures' / '..' / 'outside.png',
                     root / 'webview' / 'image.png', root / 'captures' / 'NUL.png',
                     root / 'captures' / 'text.txt', root.parent / 'outside.png'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                resolve_media_path(root, path)
        missing = root / 'captures' / 'missing.png'
        self.assertEqual(resolve_media_path(root, missing), missing)
        self.assertIsNone(existing_media_path(root, missing))
