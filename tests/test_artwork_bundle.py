"""Curated skin packaging and desktop catalogue fallback use isolated fixtures."""

import base64
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from matchlog.artwork import Artwork
from tools.build_portable import bundle_application, bundle_artwork, package_files


def skin(root, ident, *, color=(35, 180, 80, 255), opaque=False, **metadata):
    relative = f'vantage/{ident}.webp'
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGBA', (12, 10), color if opaque else (0, 0, 0, 0))
    image.paste(color, (2, 1, 10, 9))
    image.save(path, lossless=True)
    return {
        'id': ident, 'file': relative, 'name': ident, 'hero': 'vantage', 'rarity': 'Legendary',
        'source': 'https://apexlegendsrenders.com/renders/vantage/skin/',
        'attribution': 'Apex Legends Renders / EA / Respawn', 'transparent': True, **metadata,
    }


def write_index(folder, *rows):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'index.json').write_text(json.dumps({
        'images': rows, 'failed': [{'path': 'test-profile-only.db'}],
    }), encoding='utf-8')


class ArtworkBundleTests(unittest.TestCase):
    def test_bundle_includes_local_scripts_and_styles_linked_from_index(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_application(root)
            ui = root / 'ui'
            html = (ui / 'index.html').read_text(encoding='utf-8')
            references = re.findall(r'(?:src|href)="([^"/]+\.(?:js|css))"', html)
            self.assertTrue(references)
            for name in references:
                self.assertTrue((ui / name).is_file(), name)

    def test_bundle_copies_only_indexed_attributed_transparent_images(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'data/artwork/renders'
            bundled = root / 'release/ui/assets/heroes/skins'
            valid = skin(source, 'curated', size=[111, 111], private='test-profile-only.db')
            duplicate = {**skin(source, 'duplicate'), 'id': valid['id']}
            opaque = skin(source, 'opaque', opaque=True)
            missing_credit = skin(source, 'missing-credit', attribution='')
            private_link = skin(source, 'private-link', source='https://apexlegendsrenders.com/?token=private')
            low_rarity = skin(source, 'low-rarity', rarity='Rare')
            outside = {**skin(root, 'escaped'), 'file': '../../../vantage/escaped.webp'}
            bad_image = skin(source, 'bad-image')
            (source / bad_image['file']).write_bytes(b'invalid webp')
            skin(source, 'unindexed')
            (source / 'catalog.json').write_text('{"private": true}', encoding='utf-8')
            write_index(source, valid, duplicate, opaque, missing_credit, private_link,
                        low_rarity, outside, bad_image, None)

            self.assertEqual(bundle_artwork(source, bundled), 1)
            self.assertEqual({path.relative_to(bundled).as_posix() for path in bundled.rglob('*')
                              if path.is_file()}, {'index.json', valid['file']})
            self.assertEqual((bundled / valid['file']).read_bytes(), (source / valid['file']).read_bytes())
            self.assertFalse((root / 'release/data').exists())
            index = (bundled / 'index.json').read_text(encoding='utf-8')
            self.assertNotIn('private', index)
            self.assertEqual(json.loads(index)['images'][0]['size'], [12, 10])
            self.assertEqual(json.loads(index)['images'][0]['bounds'], [2, 1, 10, 9])
            gallery = Artwork(root / 'release/portable-data/artwork/renders', bundled).gallery()
            self.assertEqual(gallery[0]['attribution'], valid['attribution'])

    def test_ui_asset_copy_can_exclude_an_unreviewed_skins_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'bundle'
            skin(source / 'heroes/illustrations', 'base')
            skin(source / 'heroes/skins', 'unreviewed')
            package_files(source, target, {'.webp'}, omit={'heroes/skins'})
            self.assertTrue((target / 'heroes/illustrations/vantage/base.webp').is_file())
            self.assertFalse((target / 'heroes/skins').exists())

    def test_local_catalogue_overrides_bundled_and_missing_local_file_falls_back(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            local, bundled = root / 'local', root / 'bundled'
            local_row = skin(local, 'overlap', name='Local version', color=(20, 40, 150, 255))
            bundled_row = skin(bundled, 'overlap', name='Bundled version')
            write_index(bundled, bundled_row, skin(bundled, 'bundled-only'))
            artwork = Artwork(local, bundled_root=bundled)
            self.assertEqual(artwork.gallery()[0]['name'], 'Bundled version')

            write_index(local, local_row, skin(local, 'local-only'))
            self.assertEqual({row['id']: row['name'] for row in artwork.gallery()}, {
                'overlap': 'Local version', 'bundled-only': 'bundled-only', 'local-only': 'local-only',
            })
            self.assertEqual(base64.b64decode(artwork.image('overlap').split(',', 1)[1]),
                             (local / local_row['file']).read_bytes())
            (local / local_row['file']).unlink()
            self.assertEqual({row['id']: row['name'] for row in artwork.gallery()}['overlap'],
                             'Bundled version')
            self.assertEqual(base64.b64decode(artwork.image('overlap').split(',', 1)[1]),
                             (bundled / bundled_row['file']).read_bytes())
            with self.assertRaisesRegex(ValueError, '素材不存在'):
                artwork.image('missing')

    def test_default_absent_source_is_optional(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(bundle_artwork(root / 'absent', root / 'bundle'), 0)
            self.assertFalse((root / 'bundle').exists())
            self.assertEqual(Artwork(root / 'absent').gallery(), [])
