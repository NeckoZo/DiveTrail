"""Build Windows x64 releases with an isolated CPython distribution."""

import argparse
import hashlib
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))

from matchlog.artwork import artwork_path, read_artwork_index


PYTHON = '3.13.17'
PYTHON_SHA256 = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
OCR_MODELS = {
    'PP-OCRv6_det_small.onnx': '090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f',
    'PP-OCRv6_rec_small.onnx': '6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884',
    'ch_ppocr_mobile_v2.0_cls_mobile.onnx': 'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c',
}
UI_FILES = (
    'index.html', 'app.js', 'changelog.json', 'climb-chart.js', 'desktop.css', 'diagnostics.css',
    'hero-lines.json', 'maps.css', 'maps.js', 'match-filters.js', 'overview.js',
    'style.css', 'window-layout.js',
)
ARTWORK_ORIGINS = {'apex.source.gs', 'apexdb.gg', 'apexlegends.wiki.gg', 'apexlegendsrenders.com'}
ARTWORK_RARITIES = {'Legendary', 'Iconic', 'Mythic'}
PUBLIC_ASSETS = (
    'brand/divetrail-mark.svg', 'brand/divetrail.ico', 'brand/rank-fallback.svg',
    'brand/divetrail-mascot.svg', 'brand/divetrail-mascot.ico',
    'fonts/archivo.ttf', 'fonts/OFL.txt',
    'sounds/end.wav', 'sounds/summary.wav', 'sounds/rank.wav', 'sounds/LICENSE.txt',
)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def package_files(source, dest, suffixes, omit=()):
    omit = tuple(Path(part) for part in omit)
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if (path.is_file() and path.suffix in suffixes
                and not any(part.startswith('.') or part == '__pycache__' for part in relative.parts)
                and not any(relative.is_relative_to(part) for part in omit)):
            copy(path, dest / relative)


def bundle_artwork(source, dest):
    source = source.resolve()
    if not (source / 'index.json').is_file():
        return 0
    images = {}
    for row in read_artwork_index(source):
        rarity = row.get('rarity')
        attribution = row.get('attribution')
        link = row.get('source')
        if (row['id'] in images or row.get('transparent') is not True
                or not isinstance(rarity, str) or rarity not in ARTWORK_RARITIES
                or not isinstance(attribution, str) or not attribution.strip()
                or not isinstance(link, str)):
            continue
        relative = Path(row['file'])
        path = artwork_path(source, row)
        if len(relative.parts) != 2 or relative.parts[0] != row['hero'] or path != source / relative:
            continue
        try:
            url = urlsplit(link)
            if url.scheme != 'https' or url.netloc not in ARTWORK_ORIGINS or url.query or url.fragment:
                continue
            with Image.open(path) as image:
                if f'.{image.format.lower()}' != path.suffix.lower():
                    continue
                alpha = image.convert('RGBA').getchannel('A')
                bounds = alpha.point(lambda value: 255 if value > 20 else 0).getbbox()
                if not bounds or alpha.getextrema()[0] == 255:
                    continue
                size = image.size
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
        copy(path, dest / relative)
        images[row['id']] = {
            'id': row['id'], 'hero': row['hero'], 'name': row['name'], 'rarity': rarity,
            'file': relative.as_posix(), 'size': size, 'bounds': bounds,
            'bytes': path.stat().st_size, 'transparent': True, 'source': link, 'attribution': attribution,
        }
    if not images:
        raise ValueError('The artwork index contains no valid attributed transparent skins')
    (dest / 'index.json').write_text(json.dumps({
        'version': 1, 'images': list(images.values()),
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return len(images)


def bundle_dependencies(target):
    pending = [Requirement(line) for line in (ROOT / 'requirements.txt').read_text().splitlines()
               if line and not line.startswith('#')]
    installed = {}
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
            continue
        name = canonicalize_name(requirement.name)
        dist = distribution(name)
        if dist.version not in requirement.specifier:
            raise ValueError(f'{name}=={dist.version} does not satisfy {requirement}')
        if name in installed:
            continue
        installed[name] = dist.version
        pending.extend(Requirement(line) for line in dist.requires or ())
        for relative in dist.files or ():
            if '..' in relative.parts or '__pycache__' in relative.parts or relative.suffix == '.pyc':
                continue
            source = Path(dist.locate_file(relative))
            if source.is_file():
                copy(source, target / relative)

    rapidocr = distribution('rapidocr')
    for name, expected in OCR_MODELS.items():
        source = Path(rapidocr.locate_file(f'rapidocr/models/{name}'))
        if not source.is_file() or sha256(source) != expected:
            raise ValueError(f'Missing or mismatched local OCR model {name}')
        copy(source, target / 'rapidocr' / 'models' / name)
    (target.parent / 'requirements.lock').write_text(
        ''.join(f'{name}=={version}\n' for name, version in sorted(installed.items())), encoding='utf-8')


def bundle_runtime(archive, target):
    if sha256(archive) != PYTHON_SHA256:
        raise ValueError(f'Expected Python {PYTHON} x64 embeddable archive from python.org')
    target.mkdir()
    with ZipFile(archive) as source:
        for item in source.infolist():
            # The official embeddable archive contains only files in its root.
            if Path(item.filename).name != item.filename or item.is_dir():
                raise ValueError(f'Unexpected embedded runtime entry {item.filename}')
            (target / item.filename).write_bytes(source.read(item))
    (target / 'python313._pth').write_text(
        'python313.zip\n.\nsite-packages\n..\n', encoding='ascii')
    bundle_dependencies(target / 'site-packages')


def compile_executable(dest, *arguments):
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([
        str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
        f'/win32icon:{ROOT / "ui/assets/brand/divetrail.ico"}',
        f'/out:{dest}', *arguments,
    ], check=True)


def make_launcher(dest, installed=False):
    defines = ['/define:INSTALLED'] if installed else []
    compile_executable(dest / 'DiveTrail.exe', *defines, str(ROOT / 'tools/portable_launcher.cs'))
    entry = 'matchlog-entry.py' if installed else 'portable-entry.py'
    copy(ROOT / 'tools/portable_entry.py', dest / entry)


def zip_bundle(folder, archive, parent):
    with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=5) as result:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                result.write(path, path.relative_to(parent))


def make_version_file(version_file, version, title):
    parts = version.split('.')
    if len(parts) != 3 or not all(part.isascii() and part.isdecimal() for part in parts):
        raise ValueError('The installer requires a three-part numeric release version')
    version_file.write_text(
        f'[assembly: System.Reflection.AssemblyVersion("{version}.0")]\n'
        f'[assembly: System.Reflection.AssemblyTitle("{title}")]\n', encoding='ascii')


def make_setup(bundle, destination, version):
    payload = bundle.parent / 'payload.zip'
    zip_bundle(bundle, payload, bundle)
    version_file = bundle.parent / 'setup-version.cs'
    make_version_file(version_file, version, 'DiveTrail Setup')
    compile_executable(destination, '/r:System.IO.Compression.dll', '/r:Microsoft.CSharp.dll',
                       f'/resource:{payload},MatchLogPayload', str(version_file),
                       str(ROOT / 'tools/windows_setup.cs'))


def verify_runtime(folder):
    command = """\
from pathlib import Path
import sys
import cv2
import mss
import numpy as np
import onnxruntime
import requests
import webview
from PIL import Image, ImageDraw, ImageFont
from matchlog.recognition import Recognizer
import app
from unittest.mock import patch
root = Path.cwd().resolve()
assert Path(app.__file__).parent == root
assert all(Path(entry).resolve().is_relative_to(root) for entry in sys.path)
image = Image.new('RGB', (540, 110), 'white')
ImageDraw.Draw(image).text((20, 25), 'Apex 123', font=ImageFont.load_default(size=46), fill='black')
recognizer = Recognizer()
with patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('Unexpected network use')):
    recognizer._initialize()
    ocr = recognizer._engine(np.asarray(image))
assert any('123' in text for text in (ocr.txts or ())), ocr.txts
print('Bundled runtime, source isolation and local OCR verified')
"""
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONHOME=str(ROOT))
    subprocess.run([str(folder / 'runtime/python.exe'), '-B', '-X', 'utf8', '-c', command],
                   cwd=folder, env=env, check=True)
    assert not (folder / 'data').exists() and not (folder / 'portable-data').exists()


def bundle_application(bundle, public=False):
    copy(ROOT / 'app.py', bundle / 'app.py')
    package_files(ROOT / 'matchlog', bundle / 'matchlog', {'.py'})
    for name in UI_FILES:
        copy(ROOT / 'ui' / name, bundle / 'ui' / name)
    if public:
        for name in PUBLIC_ASSETS:
            copy(ROOT / 'ui/assets' / name, bundle / 'ui/assets' / name)
        for name in ('ui/app.js', 'matchlog/recognition.py'):
            path = bundle / name
            path.write_text(public_source(name, path.read_text(encoding='utf-8')), encoding='utf-8')
        index = bundle / 'ui/index.html'
        index.write_text(index.read_text(encoding='utf-8')
                         .replace('<html lang=', '<html data-artwork="public" lang=')
                         .replace('assets/ranks/rookie-1.png','assets/brand/rank-fallback.svg')
                         .replace('assets/heroes/illustrations/vantage.png','assets/brand/divetrail-mark.svg')
                         .replace('assets/brand/nessie-mascot.svg','assets/brand/divetrail-mascot.svg'),
                         encoding='utf-8')
    else:
        package_files(ROOT / 'ui/assets', bundle / 'ui/assets', {
            '.json', '.png', '.webp', '.jpg', '.svg', '.ico', '.txt', '.ttf', '.woff2', '.wav',
        }, omit={'brand/apex-mark.svg', 'brand/apex-matchlog.ico', 'brand/apex-matchlog-mark.svg',
                 'brand/apex-matchlog-512.png', 'heroes/skins'})
    for name in ('CHANGELOG.md','LICENSE','THIRD_PARTY_NOTICES.md','PRIVACY.md'):
        copy(ROOT / name, bundle / name)


def public_source(name, content):
    replacements = {
        'matchlog/recognition.py': ('    return key\n', '    return name\n'),
        'ui/app.js': ('const nicknameKey=key=>key;\n', 'const nicknameLabel=name=>name;\n'),
    }
    by_marker = dict(zip(('PRIVATE_NICKNAME_KEY', 'PRIVATE_NICKNAME_LABEL'), replacements[name]))
    return ''.join(next((replacement for marker, replacement in by_marker.items() if marker in line), line)
                   for line in content.splitlines(keepends=True))


def build(archive, output, artwork_source=None, installer=False, public=False):
    if (sys.platform != 'win32' or sys.version_info[:2] != (3, 13)
            or struct.calcsize('P') != 8 or sys.prefix == sys.base_prefix):
        raise ValueError('Run from an isolated Windows x64 Python 3.13 virtual environment')
    if artwork_source is not None and not (artwork_source / 'index.json').is_file():
        raise ValueError('The artwork source must contain index.json')
    if public and artwork_source is not None:
        raise ValueError('The public package only uses assets on the release allowlist')
    version = json.loads((ROOT / 'ui/changelog.json').read_text(encoding='utf-8'))['version']
    output.mkdir(parents=True, exist_ok=True)
    edition = 'setup' if installer else 'portable'
    bundle_name = f'DiveTrail-{version}-{edition}-win-x64'
    release = output / (f'{bundle_name}.exe' if installer else f'{bundle_name}.zip')
    with TemporaryDirectory(prefix='matchlog-build-', dir=output) as work:
        work = Path(work)
        bundle = work / bundle_name
        bundle.mkdir()
        bundle_application(bundle, public)
        skin_count = bundle_artwork(artwork_source, bundle / 'ui/assets/heroes/skins') if artwork_source else 0
        readme = 'INSTALL-START.md' if installer else 'PORTABLE-START.md'
        copy(ROOT / 'docs' / readme, bundle / 'README.md')
        bundle_runtime(archive, bundle / 'runtime')
        make_launcher(bundle, installed=installer)
        verify_runtime(bundle)
        temporary_release = work / release.name
        if installer:
            make_setup(bundle, temporary_release, version)
        else:
            zip_bundle(bundle, temporary_release, work)
        temporary_release.replace(release)
    print(json.dumps({'path': str(release), 'bytes': release.stat().st_size,
                      'sha256': sha256(release), 'bundled_skins': skin_count}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/release')
    parser.add_argument('--artwork-source', type=Path, help='Curated renders folder containing index.json')
    parser.add_argument('--installer', action='store_true', help='Build a per-user Windows setup executable')
    parser.add_argument('--public', action='store_true', help='Use the public release asset allowlist')
    args = parser.parse_args()
    build(args.python_zip.resolve(strict=True), args.output.resolve(), args.artwork_source, args.installer, args.public)
