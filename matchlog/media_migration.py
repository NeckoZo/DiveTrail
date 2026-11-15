"""Rename local capture files while keeping history and rollback copies consistent."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import NamedTemporaryFile

from matchlog.capture_journal import _review_retry_key
from matchlog.image_storage import CHINA, capture_name
from matchlog.media_paths import LEGACY_LAYOUT, MEDIA_LAYOUT, media_dir, media_dirs, resolve_media_path
from matchlog.migrations import backup_database
from matchlog.record_merge import UNDO


FLAG = 'media_layout_v2'
CLEANUP_FLAG = 'media_layout_v2_cleanup'
TABLE_COLUMNS = {
    'matches': ('id', 'created', 'payload'),
    'evidence': ('match_id', 'kind', 'path'),
    'capture_jobs': ('created', 'kind', 'path', 'round_id', 'payload'),
    'hud_backups': ('created', 'path', 'payload'),
    'lobby_rank': ('created', 'path', 'payload'),
    'lobby_history': ('created', 'path', 'payload'),
    'rp_checks': ('created', 'payload'),
    'profile_snapshots': ('created', 'path', 'payload'),
    'match_highlights': ('created', 'path', 'payload'),
    'media_asset_paths': ('category', 'path'),
}
DEFAULT_KINDS = {
    'draft': 'queue', 'hud': 'hud', 'lobby': 'lobby', 'profiles': 'profile',
    'restored': 'restored', 'highlights': 'highlight', 'rank': 'rank', 'summary': 'summary',
}
SAMPLES = frozenset(('hero_samples', 'reference_samples'))
INCIDENT_TYPES = {'rank': ('rank', 'rank'), 'rank_overview': ('rank', 'rank-overview'),
                  'lobby': ('lobby', 'lobby'), 'summary': ('summary', 'summary')}
STAMPED = re.compile(
    r'(?P<prefix>[a-z]+(?:[-_][a-z]+)*)-(?P<date>[0-9]{8}|[0-9]{6})-'
    r'(?P<time>[0-9]{6})(?:-(?P<serial>[0-9]{2,}))?(?P<incident>-incident)?'
)
GROUPS = {parts: kind for kind, parts in MEDIA_LAYOUT.items()} | {
    parts: kind for kind, paths in LEGACY_LAYOUT.items() for parts in paths
}


def _digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _timestamp(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA)
        except ValueError:
            return None
    if type(value) in (float, int):
        try:
            return datetime.fromtimestamp(value, CHINA)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _rows(db):
    for table, columns in TABLE_COLUMNS.items():
        for raw in db.execute(f'SELECT rowid,{",".join(columns)} FROM {table}'):
            row = dict(zip(('rowid', *columns), raw))
            row['table'] = table
            if row.get('payload'):
                row['payload'] = json.loads(row['payload'])
            yield row
    # Merge undo stores the same match payload as a JSON string inside its snapshots.
    saved = db.execute('SELECT value FROM recorder_flags WHERE name=?', (UNDO,)).fetchone()
    if saved:
        undo = json.loads(saved[0])
        for section in ('before', 'after'):
            for ident, snapshot in undo.get(section, {}).items():
                yield {'table': UNDO, 'id': ident, 'created': snapshot[0], 'payload': json.loads(snapshot[1]),
                       'snapshot': snapshot, 'undo': undo}


def _payload_references(payload, at):
    if isinstance(payload, dict):
        for field, value in payload.items():
            if field in ('path', 'image_path', 'squad_path') and isinstance(value, str):
                hint = 'hud-squad' if field == 'squad_path' else payload.get('kind')
                yield payload, field, hint, at
            elif field == 'evidence' and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        kind = item.get('kind')
                        yield from _payload_references(item, _timestamp(payload.get(f'{kind}_captured_at')) or at)
            elif isinstance(value, (dict, list)):
                yield from _payload_references(value, at)
    elif isinstance(payload, list):
        for item in payload:
            yield from _payload_references(item, at)


def _references(row, matches):
    """Yield the owning dict, field, category hint, and best stored timestamp."""
    at = _timestamp(row.get('created'))
    if 'path' in row:
        kind = row.get('kind') or row.get('category')
        if row['table'] == 'evidence':
            match = matches.get(row['match_id'], {})
            match_data = match.get('payload')
            if isinstance(match_data, dict):
                at = _timestamp(match_data.get(f'{kind}_captured_at'))
            at = at or _timestamp(match.get('created'))
        yield row, 'path', kind, at
    yield from _payload_references(row.get('payload'), at)


def _observations(rows, root):
    matches = {row['id']: row for row in rows if row['table'] == 'matches'}
    refs = {}
    hud_squads = {}
    paths = {}
    for row in rows:
        for owner, field, kind, at in _references(row, matches):
            value = owner[field]
            if not isinstance(value, str):
                continue
            if value not in paths:
                try:
                    paths[value] = resolve_media_path(root, value)
                except ValueError:
                    paths[value] = None
            path = paths[value]
            if path is None:
                continue
            before_kind, before_at = refs.get(path, (None, None))
            refs[path] = (before_kind or kind, before_at or at)
            if row['table'] == 'hud_backups' and owner is row and path.parent == root / 'hud':
                squad = path.with_name(path.stem + '-squad' + path.suffix)
                if squad.is_file() and squad.resolve() == squad:
                    refs.setdefault(squad, ('hud-squad', at))
                    hud_squads[value] = squad
    return matches, refs, paths, hud_squads


def _media_files(root):
    for kind in (*MEDIA_LAYOUT, 'captures'):
        for base in media_dirs(root, kind):
            if not base.is_dir() or base.resolve() != base:
                continue
            if kind in SAMPLES and base == media_dir(root, kind):
                continue
            for dirname, dirs, files in os.walk(base, followlinks=False):
                directory = Path(dirname)
                dirs[:] = sorted(name for name in dirs if (directory / name).resolve() == directory / name)
                for name in sorted(files):
                    path = directory / name
                    try:
                        yield resolve_media_path(root, path)
                    except ValueError:
                        if kind == 'hero_samples' and path == base / 'index.json' and path.resolve() == path:
                            yield path


def _group(root, path):
    folders = path.relative_to(root).parts[:-1]
    parts = folders[:2] if folders[:2] in GROUPS else folders[:1]
    return GROUPS[parts], root.joinpath(*parts)


def _name_parts(source, folder, hint, at, incident_kinds):
    stamp = STAMPED.fullmatch(source.stem)
    prefix = stamp['prefix'] if stamp else source.stem.split('-', 1)[0]
    incident = prefix.startswith('incident-') or bool(stamp and stamp['incident'])
    if incident:
        key = incident_kinds.get(source.name) or prefix.removeprefix('incident-').replace('-', '_')
        if key not in INCIDENT_TYPES:
            return None
        folder, kind = INCIDENT_TYPES[key]
    elif folder in ('captures', 'rank') and prefix.replace('-', '_') == 'rank_overview':
        folder, kind = INCIDENT_TYPES['rank_overview']
    elif folder == 'captures':
        kind = prefix if prefix in ('rank', 'summary') else hint
        if kind not in ('rank', 'summary'):
            return None
        folder = kind
    else:
        kind = DEFAULT_KINDS[folder]
        if folder == 'hud' and (hint == 'hud-squad' or 'hud-squad' == prefix or source.stem.endswith('-squad')):
            kind = 'hud-squad'
    serial = 1
    if stamp:
        date = stamp['date']
        fmt = '%Y%m%d' if len(date) == 8 else '%y%m%d'
        try:
            at = datetime.strptime(date + stamp['time'], fmt + '%H%M%S').replace(tzinfo=CHINA)
        except ValueError:
            pass
        serial = int(stamp['serial'] or '1')
    if at is None and source.is_file():
        at = datetime.fromtimestamp(source.stat().st_mtime, CHINA)
    return folder, kind, at, serial, incident


def _same_file(path, digest, mtime_ns):
    return (digest is not None and path.is_file() and path.stat().st_mtime_ns == mtime_ns
            and _digest(path) == digest)


def _time_target(folder, kind, at, suffix, serial, incident, reserved, digest, mtime_ns):
    while True:
        target = folder / capture_name(kind, at, suffix, serial, incident=incident)
        if target not in reserved:
            if target.resolve() != target:
                raise ValueError('迁移截图路径不安全')
            if not target.exists() or _same_file(target, digest, mtime_ns):
                return target
        serial += 1


def _copy_verified(source, target, digest):
    if source.resolve() != source or target.resolve() != target:
        raise ValueError('迁移截图路径不安全')
    mtime_ns = source.stat().st_mtime_ns
    if target.exists():
        if not _same_file(target, digest, mtime_ns):
            raise FileExistsError(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, prefix='.media-move-', delete=False) as output:
        temporary = Path(output.name)
    try:
        shutil.copy2(source, temporary)
        if not _same_file(temporary, digest, mtime_ns):
            raise OSError('迁移截图校验失败')
        # Publish an already verified copy without replacing an existing file.
        if os.name == 'nt':
            temporary.rename(target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _plan(root, refs, incident_kinds, report):
    known = set(_media_files(root))
    sources = sorted(known | refs.keys())
    reserved = set(refs)
    old_index = root / 'hero_samples' / 'index.json'
    new_index = media_dir(root, 'hero_samples') / 'index.json'
    keep_legacy_heroes = old_index in known and new_index.exists() and (
        new_index.resolve() != new_index or not new_index.is_file() or _digest(old_index) != _digest(new_index)
    )
    for source in sources:
        folder, base = _group(root, source)
        if folder in SAMPLES:
            if base == media_dir(root, folder) or source not in known:
                continue
            if folder == 'hero_samples' and keep_legacy_heroes:
                report['retained'] += 1
                continue
            target = media_dir(root, folder) / source.relative_to(base)
        else:
            current = base == root.joinpath(*MEDIA_LAYOUT.get(folder, ()))
            stamp = STAMPED.fullmatch(source.stem)
            if current and not (stamp and (len(stamp['date']) == 8 or stamp['prefix'].startswith('incident-'))):
                continue
            hint, at = refs.get(source, (None, None))
            parts = _name_parts(source, folder, hint, at, incident_kinds)
            if parts is None:
                report['retained'] += source in known
                continue
            category, kind, at, serial, incident = parts
            if source.name.startswith('portable-'):
                at = None
            if at is None:
                target = media_dir(root, category) / source.name
        digest = _digest(source) if source in known else None
        mtime_ns = source.stat().st_mtime_ns if source in known else None
        if folder not in SAMPLES and at is not None:
            target = _time_target(media_dir(root, category), kind, at, source.suffix.lower(),
                                  serial, incident, reserved, digest, mtime_ns)
        elif (target.resolve() != target or (target.exists() and not _same_file(target, digest, mtime_ns))):
            report['retained'] += source in known
            continue
        reserved.add(target)
        yield source, target, digest, mtime_ns


def _rewrite_rows(db, rows, matches, paths, moved, hud_squads):
    retries = set()
    undo = None
    for row in rows:
        previous_path = row.get('path')
        payload_changed = False
        for owner, field, _, _ in _references(row, matches):
            value = owner[field]
            target = moved.get(paths.get(value)) if isinstance(value, str) else None
            if target:
                owner[field] = str(target)
                payload_changed |= owner is not row
        table = row['table']
        if table == 'hud_backups' and isinstance(row.get('payload'), dict) and not row['payload'].get('squad_path'):
            squad = moved.get(hud_squads.get(previous_path))
            if squad:
                row['payload']['squad_path'] = str(squad)
                payload_changed = True
        if row.get('path') != previous_path:
            db.execute(f'UPDATE {table} SET path=? WHERE rowid=?', (row['path'], row['rowid']))
            if table == 'capture_jobs':
                retries.add((_review_retry_key(previous_path, row['round_id']),
                             _review_retry_key(row['path'], row['round_id'])))
        if payload_changed:
            payload = json.dumps(row['payload'], ensure_ascii=False)
            if table == UNDO:
                row['snapshot'][1] = payload
                undo = row['undo']
            else:
                db.execute(f'UPDATE {table} SET payload=? WHERE rowid=?', (payload, row['rowid']))
    if undo is not None:
        db.execute('UPDATE recorder_flags SET value=? WHERE name=?', (json.dumps(undo, ensure_ascii=False), UNDO))
    db.executemany("""INSERT INTO recognition_retries (key,attempts)
        SELECT ?,attempts FROM recognition_retries WHERE key=?
        ON CONFLICT(key) DO UPDATE SET attempts=MAX(attempts,excluded.attempts)""",
                   [(new, old) for old, new in retries if old != new])
    db.executemany('DELETE FROM recognition_retries WHERE key=?',
                   [(old,) for old, new in retries if old != new])


def _manifest_path(root, name):
    if not re.fullmatch(r'before-media-layout-[0-9T]+Z\.media-map\.json', name):
        raise ValueError('迁移备份路径不安全')
    path = root / 'backups' / name
    if path.resolve() != path:
        raise ValueError('迁移备份路径不安全')
    return path


def _manifest_media(root, relative):
    path = root / relative
    if path in (root / 'hero_samples' / 'index.json', media_dir(root, 'hero_samples') / 'index.json'):
        if path.resolve() == path:
            return path
    return resolve_media_path(root, path)


def _finish_cleanup(db, root, report):
    pending = db.execute('SELECT value FROM recorder_flags WHERE name=?', (CLEANUP_FLAG,)).fetchone()
    if not pending:
        return True
    manifest = json.loads(_manifest_path(root, pending[0]).read_text(encoding='utf-8'))
    with db:
        db.execute('BEGIN IMMEDIATE')
        _, refs, _, _ = _observations(_rows(db), root)
        retry = False
        directories = set()
        for entry in manifest:
            if not entry['sha256']:
                continue
            source = _manifest_media(root, entry['from'])
            target = _manifest_media(root, entry['to'])
            try:
                if not source.exists():
                    continue
                if (source != target and source not in refs
                        and _same_file(source, entry['sha256'], entry['mtime_ns'])
                        and _same_file(target, entry['sha256'], entry['mtime_ns'])):
                    source.unlink()
                    report['removed'] += 1
                    directories.update(directory for directory in source.parents
                                       if directory.is_relative_to(root) and directory != root)
                    continue
            except OSError:
                retry = True
            report['retained'] += 1
        if not retry:
            db.execute('DELETE FROM recorder_flags WHERE name=?', (CLEANUP_FLAG,))
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        if directory.resolve() == directory:
            try:
                directory.rmdir()
            except OSError:
                pass
    return not retry


def migrate_media_layout(store, *, incident_kinds=None):
    """Copy and verify captures, commit references, then remove verified originals."""
    root = store.root.resolve()
    report = {'moved': 0, 'removed': 0, 'retained': 0, 'missing': 0, 'backup': None}
    with store.lock:
        db = store.db
        if not _finish_cleanup(db, root, report):
            return report
        if not incident_kinds and db.execute('SELECT 1 FROM recorder_flags WHERE name=?', (FLAG,)).fetchone():
            return report
        if (root / 'backups').resolve() != root / 'backups':
            raise ValueError('迁移备份路径不安全')
        # Detect writes by another connection before applying the plan and its backup.
        version = db.execute('PRAGMA data_version').fetchone()[0]
        rows = list(_rows(db))
        matches, refs, paths, hud_squads = _observations(rows, root)
        plan = list(_plan(root, refs, incident_kinds or {}, report))
        if plan:
            backup = backup_database(store, 'before-media-layout')
            report['backup'] = str(backup)
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('PRAGMA data_version').fetchone()[0] != version:
                raise OSError('战绩库正在写入，请重试截图迁移')
            if plan:
                manifest = []
                moved = {}
                for source, target, digest, mtime_ns in plan:
                    if digest:
                        _copy_verified(source, target, digest)
                        report['moved'] += 1
                    else:
                        report['missing'] += 1
                    moved[source] = target
                    manifest.append({'from': source.relative_to(root).as_posix(),
                                     'to': target.relative_to(root).as_posix(),
                                     'sha256': digest, 'mtime_ns': mtime_ns})
                manifest_path = _manifest_path(root, backup.with_suffix('.media-map.json').name)
                with manifest_path.open('x', encoding='utf-8') as file:
                    json.dump(manifest, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                _rewrite_rows(db, rows, matches, paths, moved, hud_squads)
                db.execute('INSERT OR REPLACE INTO recorder_flags VALUES (?,?)', (CLEANUP_FLAG, manifest_path.name))
            db.execute('INSERT OR REPLACE INTO recorder_flags VALUES (?,1)', (FLAG,))
        _finish_cleanup(db, root, report)
    return report
