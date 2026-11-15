"""Verify a release package in an isolated Windows directory."""

import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / 'ui/changelog.json').read_text(encoding='utf-8'))['version']
SKINS = 'ui/assets/heroes/skins'


def wait_until(check, description, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.15)
    raise TimeoutError(f'timed out waiting for {description}')


def verify_window(bundle, env):
    title_hash = hashlib.sha256(str(bundle).casefold().encode()).hexdigest()[:6]
    title = f'DiveTrail · 尾迹 · 便携版 · {bundle.name} [{title_hash}]'
    user32 = ctypes.WinDLL('user32')
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW.restype = wintypes.BOOL

    def window():
        return user32.FindWindowW(None, title)

    assert not (bundle / 'portable-data').exists()
    subprocess.run([str(bundle / 'DiveTrail.exe')], env=env, timeout=15, check=True)
    hwnd = wait_until(window, 'native launcher window', timeout=30)
    try:
        database = bundle / 'portable-data' / 'matches.sqlite3'
        wait_until(database.is_file, 'new portable database')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            assert db.execute('SELECT COUNT(*) FROM matches').fetchone()[0] == 0
            for raw, in db.execute('SELECT payload FROM settings'):
                settings = json.loads(raw)
                assert not settings.get('self_name') and not settings.get('friends')
        assert not (bundle / 'data').exists()
    finally:
        assert user32.PostMessageW(hwnd, 0x0010, 0, 0)
        wait_until(lambda: not window(), 'portable window to close')
    print('Native launcher created an isolated empty database', flush=True)


def verify_bundle(bundle, *, public, installer, prepare):
    from tools.build_portable import PUBLIC_ASSETS, UI_FILES, bundle_application

    asset_root = bundle / 'ui/assets'
    files = {path.relative_to(bundle).as_posix() for path in bundle.rglob('*') if path.is_file()}
    assert not {path.split('/')[0] for path in files} & {
        '.git', '.venv', 'data', 'portable-data', 'tests', 'output', 'cloudbase',
    }
    for name in (
        'DiveTrail.exe', 'runtime/python.exe', 'runtime/pythonw.exe', 'runtime/requirements.lock',
        'matchlog-entry.py' if installer else 'portable-entry.py', 'README.md', 'LICENSE',
        'CHANGELOG.md', 'THIRD_PARTY_NOTICES.md', 'PRIVACY.md',
    ):
        assert name in files, name
    assert ('matchlog-entry.py' if not installer else 'portable-entry.py') not in files
    assert ('.divetrail-install' in files) == installer
    expected_readme = ROOT / 'docs' / ('INSTALL-START.md' if installer else 'PORTABLE-START.md')
    assert (bundle / 'README.md').read_bytes() == expected_readme.read_bytes()
    if installer:
        assert (bundle / '.divetrail-install').read_text(encoding='ascii') == VERSION + '.0'

    assert (asset_root / 'brand/divetrail-mark.svg').is_file()
    if public:
        # Generate the allowlist from the current source. This also performs the same
        # privacy substitutions as the release build while keeping tests out of the package.
        bundle_application(prepare, public=True)
        assert {path.relative_to(asset_root).as_posix() for path in asset_root.rglob('*')
                if path.is_file()} == set(PUBLIC_ASSETS)
        assert set(path for path in files if path.startswith('matchlog/')) == {
            path.relative_to(prepare).as_posix() for path in (prepare / 'matchlog').rglob('*.py')
        }
        expected = prepare
    else:
        expected = ROOT
        assert (asset_root / 'brand/nessie-mascot.ico').is_file()
    sources = ('app.py', *(f'ui/{name}' for name in UI_FILES),
               *(name for name in files if name.startswith('matchlog/')))
    if public:
        sources += tuple(f'ui/assets/{asset}' for asset in PUBLIC_ASSETS)
    sources += ('CHANGELOG.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'PRIVACY.md')
    for path in sources:
        assert (bundle / path).read_bytes() == (expected / path).read_bytes(), path

    if not public and (bundle / SKINS).is_dir():
        artwork = bundle / SKINS
        skins = json.loads((artwork / 'index.json').read_text(encoding='utf-8'))['images']
        assert len(skins) == len({row['id'] for row in skins}) > 0
        assert all(row['attribution'] and row['source'].startswith('https://') and row['transparent']
                   for row in skins)
        assert {path.relative_to(artwork).as_posix() for path in artwork.rglob('*') if path.is_file()} == {
            'index.json', *(row['file'] for row in skins),
        }
        assert all((artwork / row['file']).stat().st_size == row['bytes'] for row in skins)


def clean_env():
    return dict(os.environ, PYTHONPATH=str(ROOT), PYTHONHOME=str(ROOT),
               PATH=os.pathsep.join([str(Path(os.environ['WINDIR']) / 'System32'), os.environ['WINDIR']]))


def verify_zip(zip_path, *, public, contents_only):
    output = ROOT / 'output'
    output.mkdir(exist_ok=True)
    env = clean_env()
    with tempfile.TemporaryDirectory(prefix='便携验证 ', dir=output,
                                     ignore_cleanup_errors=True) as scratch:
        scratch = Path(scratch).resolve()
        assert scratch.is_relative_to(output)
        with ZipFile(zip_path) as archive:
            names = archive.namelist()
            members = [PurePosixPath(name) for name in names]
            assert len(names) == len({name.casefold() for name in names})
            assert len({member.parts[0] for member in members}) == 1
            root_name = members[0].parts[0]
            assert root_name == f'DiveTrail-{VERSION}-portable-win-x64'
            assert all(len(path.parts) > 1 and not path.is_absolute() and '..' not in path.parts
                       and '\\' not in name and ':' not in name for path, name in zip(members, names))
            if not contents_only:
                archive.extractall(scratch / 'launcher')
            archive.extractall(scratch / 'webview')
        bundle = scratch / 'webview' / root_name
        verify_bundle(bundle, public=public, installer=False, prepare=scratch / 'expected')
        print(f'Portable {VERSION} contents verified', flush=True)
        if not contents_only:
            verify_window(scratch / 'launcher' / root_name, env)
            command = [str(bundle / 'runtime/python.exe'), '-B', '-X', 'utf8',
                       str(Path(__file__).resolve()), '--extracted']
            subprocess.run(command + (['--public'] if public else []), env=env, timeout=75, check=True)


def verify_setup(setup_path, *, public, contents_only):
    from tools.build_portable import verify_runtime

    output = ROOT / 'output'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='安装验证 ', dir=output) as scratch:
        scratch = Path(scratch).resolve()
        assert scratch.is_relative_to(output)
        bundle = scratch / 'staging' / 'DiveTrail'
        shell = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
$assembly = [System.Reflection.Assembly]::LoadFrom($env:DIVETRAIL_VERIFY_SETUP)
$flags = [System.Reflection.BindingFlags]::Static -bor [System.Reflection.BindingFlags]::NonPublic
$install = $assembly.GetType('WindowsSetup', $true).GetMethod('Install', $flags)
$payload = $assembly.GetManifestResourceStream('MatchLogPayload')
try {
    $install.Invoke($null, @($payload, $env:DIVETRAIL_VERIFY_TARGET, $assembly.GetName().Version))
} finally { $payload.Dispose() }
'''
        env = dict(clean_env(), DIVETRAIL_VERIFY_SETUP=str(setup_path), DIVETRAIL_VERIFY_TARGET=str(bundle))
        subprocess.run([str(shell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                       env=env, timeout=90, check=True)
        verify_bundle(bundle, public=public, installer=True, prepare=scratch / 'expected')
        print(f'Setup {VERSION} invoked installation into an isolated directory; payload verified', flush=True)
        if not contents_only:
            verify_runtime(bundle)


def verify_extracted(public):
    import app

    root = app.ROOT
    assert (root / 'portable-entry.py').is_file()
    assert Path(sys.executable).is_relative_to(root / 'runtime')
    result = {}
    report = root / 'portable-data' / 'smoke-diagnostic.json'

    class ProbeApi(app.Api):
        def check_release_update(self):
            return {'status': 'available', 'version': '99.0.0'}

        def _start(self):
            self._window.create_file_dialog = lambda *args, **kwargs: [str(report)]
            super()._start()
            threading.Thread(target=self.probe, daemon=True).start()

        def probe(self):
            try:
                eval_js = self._window.evaluate_js
                wait_until(lambda: eval_js(
                    f"""Boolean(window.pywebview?.api?.export_capture_diagnostics &&
                        document.querySelector('#changelog-btn')?.textContent === '{VERSION}' &&
                        document.querySelector('#profile-name')?.textContent === '待设置' &&
                        document.querySelector('#usage-dialog')?.open &&
                        document.querySelector('#profile-notice')?.hidden === false &&
                        document.querySelector('#profile-setup')?.hidden === false)"""), 'first-use card')
                assert eval_js("""document.querySelector('#usage-dialog kbd').textContent === 'F8' &&
                                   !document.querySelector('#profile-setup-dialog').open""")
                eval_js("document.querySelector('#usage-return').click()")
                assert eval_js("""document.querySelector('#profile-setup-dialog').open &&
                                   document.querySelector('#profile-scan-btn').textContent === '开始识别' &&
                                   document.querySelector('#profile-setup-form [name=self_name]').value === ''""")
                eval_js("document.querySelector('#profile-setup-dialog').close()")
                eval_js("document.querySelector('#usage-btn').click()")
                assert eval_js("""document.querySelector('#usage-dialog').open &&
                                   document.querySelector('#usage-dialog kbd').textContent === 'F8'""")
                eval_js("document.querySelector('#usage-dialog').close()")
                wait_until(lambda: eval_js("""!document.querySelector('#release-update').hidden &&
                           document.querySelector('#release-update').textContent.includes('99.0.0')"""),
                           'update reminder')
                eval_js("""void window.pywebview.api.hero_gallery().then(async rows => {
                    const image = rows.length ? await window.pywebview.api.hero_artwork(rows[0].id) : '';
                    window.__skinProbe = JSON.stringify({ count: rows.length,
                        unique: new Set(rows.map(row => row.id)).size,
                        credited: rows.every(row => !!row.attribution),
                        loaded: /^data:image\\/(webp|png);base64,/.test(image) });
                }).catch(error => { window.__skinProbe = JSON.stringify({error: String(error)}) })""")
                skins = json.loads(wait_until(lambda: eval_js('window.__skinProbe || null'), 'skin gallery bridge'))
                assert 'error' not in skins and skins['count'] == skins['unique'] and skins['credited'], skins
                if public:
                    assert skins['count'] == 0, skins
                elif skins['count']:
                    assert skins['loaded'], skins
                eval_js("document.querySelector('#capture-diagnostics-btn').click()")
                wait_until(lambda: eval_js(
                    """document.querySelector('#capture-diagnostics-dialog').open &&
                       document.querySelector('#capture-diagnostics-text').value.startsWith('DiveTrail · 尾迹 采集日志')"""),
                    'diagnostics dialog')
                eval_js("document.querySelector('#capture-diagnostics-export').click()")
                wait_until(report.is_file, 'diagnostic report')
                raw = report.read_text(encoding='utf-8')
                payload = json.loads(raw)
                assert payload['format'] == 'matchlog.capture-diagnostics'
                assert payload['schema'] == 1 and payload['version'] == VERSION
                assert payload['portable'] is True and payload['profile_configured'] is False
                assert payload['matches'] == [] and not payload['files']
                assert payload['environment']
                assert root.as_posix().casefold() not in raw.replace(chr(92)*2, '/').casefold()
                result['ok'] = True
            except Exception as error:
                import traceback
                result['error'] = repr(error)
                traceback.print_exc()
                print(self._window.evaluate_js("""JSON.stringify({url: location.href,
                    ready: document.readyState, bridge: !!window.pywebview?.api?.export_capture_diagnostics,
                    version: document.querySelector('#changelog-btn')?.textContent,
                    profile: document.querySelector('#profile-name')?.textContent,
                    setup: document.querySelector('#profile-setup')?.hidden,
                    notice: document.querySelector('#profile-notice')?.hidden,
                    refresh: typeof refresh})"""), flush=True)
            finally:
                self._window.destroy()

    app.Api = ProbeApi
    sys.argv = ['app.py', '--portable']
    app.main(instance_name=r'Local\MatchLogPackagedProbe')
    assert result.get('ok'), result
    print(f'Packaged {VERSION} UI verified: first-use setup, F8 guide, update reminder, and sanitized diagnostics export')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path, nargs='?')
    parser.add_argument('--public', action='store_true', help='Verify the public allowlist')
    parser.add_argument('--installer', action='store_true', help='Verify an installer EXE instead of a portable ZIP')
    parser.add_argument('--contents-only', action='store_true', help='Skip launching the portable UI or setup OCR smoke')
    parser.add_argument('--extracted', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.extracted:
        verify_extracted(args.public)
    else:
        if args.package is None:
            parser.error('a release package is required')
        # The builder and its optional dependencies are only needed in the outer verifier,
        # since the inner verifier runs inside the isolated bundled runtime.
        sys.path.insert(0, str(ROOT))
        verify = verify_setup if args.installer else verify_zip
        verify(args.package.resolve(strict=True), public=args.public, contents_only=args.contents_only)
