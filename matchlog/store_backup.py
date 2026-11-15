"""Bounded, validated portable backups with rollback-safe restores."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory
import zipfile

from matchlog.backup_extras import MAX_EXTRAS_BYTES, export_extras, prepare_extras, restore_extras
from matchlog.heroes import HeroSamples
from matchlog.media_paths import IMAGE_SUFFIXES, media_dir, resolve_media_path


TABLES = (
    'settings', 'matches', 'evidence', 'hud_backups', 'lobby_rank', 'rp_checks',
    'profile_snapshots', 'lobby_history', 'capture_rounds', 'match_highlights',
)
TABLES_V2 = (*TABLES, 'capture_jobs')
QUEUE_STATES = ('pending', 'processing', 'deferred', 'review', 'failed')
QUEUE_KINDS = ('settlement', 'manual', 'profile', 'hud', 'lobby', 'probe')
MAX_ENTRIES = 30_000
MAX_EXPANDED_BYTES = 2_000_000_000
MAX_MANIFEST_BYTES = 64 * 1024
MAX_RECORDS_BYTES = 64 * 1024 * 1024
MAX_ROW_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
CHUNK_SIZE = 1024 * 1024
MEDIA_NAME = re.compile(r'media/[a-f0-9]{64}\.(?:png|jpg|jpeg|bmp|webp)')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}')

TABLE_FIELDS = {
    'settings': {'id', 'payload'},
    'matches': {'id', 'created', 'payload'},
    'evidence': {'hash', 'match_id', 'kind', 'path'},
    'hud_backups': {'id', 'created', 'season', 'split', 'payload', 'path'},
    'lobby_rank': {'scope', 'created', 'payload', 'path'},
    'rp_checks': {'id', 'scope', 'created', 'payload'},
    'profile_snapshots': {'id', 'scope', 'created', 'payload', 'path'},
    'lobby_history': {'id', 'scope', 'created', 'payload', 'path'},
    'capture_rounds': {'id', 'scope', 'started', 'seen', 'phase'},
    'match_highlights': {'match_id', 'path', 'hash', 'created', 'payload'},
    'capture_jobs': {'id', 'scope', 'created', 'kind', 'path', 'status', 'manual'},
}
PLAYER_COUNTS = (
    'kills', 'assists', 'knocks', 'damage', 'participation', 'revives', 'respawns', 'deaths',
)
RANK_NUMBERS = ('delta', 'total_rp', 'progress', 'capacity', 'remaining', 'read_delta', 'placement')
MATCH_DATES = (
    'time', 'updated_at', 'summary_captured_at', 'rank_captured_at', 'snapshot_at',
)


def _field_error():
    return ValueError('备份记录字段不正确')


def _number(value):
    return type(value) in (int, float) and -1e15 < value < 1e15 and math.isfinite(value)


def _text(value):
    return isinstance(value, str) and len(value) <= 4096 and '\x00' not in value


def _identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _date(value):
    if not _text(value):
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def _optional_fields(row, fields, check):
    if any(row.get(field) is not None and not check(row[field]) for field in fields):
        raise _field_error()


def _validate_rank(rank):
    if not isinstance(rank, dict):
        raise _field_error()
    _optional_fields(rank, RANK_NUMBERS, _number)
    _optional_fields(rank, ('rank', 'source', 'next_rank', 'label', 'delta_source'), _text)
    _optional_fields(rank, ('captured_at',), _date)
    for field in ('breakdown', 'demotion_protection'):
        values = rank.get(field)
        if values is not None and (not isinstance(values, dict) or any(
                value is not None and not _number(value) for value in values.values())):
            raise _field_error()


def _validate_players(players):
    if not isinstance(players, list) or len(players) > 3:
        raise _field_error()
    for player in players:
        if not isinstance(player, dict) or not _text(player.get('name')):
            raise _field_error()
        _optional_fields(player, PLAYER_COUNTS, lambda v: _number(v) and 0 <= v <= 100_000)
        _optional_fields(player, ('hero', 'hero_source', 'hero_sample', 'hero_candidate', 'survival'), _text)


def _validate_match(payload, row):
    required = {'id', 'created', 'time', 'season', 'split', 'summary', 'rank', 'evidence'}
    if (not required.issubset(payload) or payload['id'] != row['id']
            or payload['created'] != row['created'] or not _number(payload['created'])
            or not _date(payload['time']) or not all(_text(payload[k]) and payload[k] for k in ('season', 'split'))):
        raise _field_error()
    _optional_fields(payload, MATCH_DATES, _date)
    _optional_fields(payload, ('account', 'mode', 'record_origin', 'record_source', 'map_name'), _text)
    _optional_fields(payload, ('round_id', 'merged_into'), _identifier)
    _optional_fields(payload, ('_snapshot_created', 'deleted_at'), _number)
    _optional_fields(payload, ('rp_order_at',), lambda v: _number(v) or _date(v))
    summary = payload['summary']
    if summary is not None:
        if not isinstance(summary, dict):
            raise _field_error()
        _validate_players(summary.get('players'))
        _optional_fields(summary, ('placement',), lambda v: type(v) is int and 1 <= v <= 30)
        _optional_fields(summary, ('squad_kills',), lambda v: _number(v) and 0 <= v <= 100_000)
    if payload['rank'] is not None:
        _validate_rank(payload['rank'])
    evidence = payload['evidence']
    if not isinstance(evidence, list) or len(evidence) > 100:
        raise _field_error()
    if any(not isinstance(item, dict) or item.get('kind') not in ('summary', 'rank')
           or not isinstance(item.get('path'), str) for item in evidence):
        raise _field_error()
    audit = payload.get('audit')
    if audit is not None and not isinstance(audit, dict):
        raise _field_error()


def _validate_settings(payload):
    from matchlog.config import HIGHLIGHT_LIMITS, MAX_RETENTION_DAYS, RETENTION_LIMITS, THEMES
    _optional_fields(payload, ('self_name',), _text)
    if any(key in payload and (not _text(payload[key]) or not payload[key].strip())
           for key in ('season', 'split')):
        raise _field_error()
    _optional_fields(payload, ('theme',), lambda v: v in THEMES)
    _optional_fields(payload, ('initial_rp',), lambda v: _number(v) and 0 <= v <= 2_000_000)
    _optional_fields(payload, ('initial_rp_at',), lambda v: _number(v) or _date(v))
    friends = payload.get('friends', [])
    if not isinstance(friends, list) or len(friends) > 20 or not all(_text(friend) for friend in friends):
        raise _field_error()
    _optional_fields(payload, ('auto_season', 'auto_switch', 'start_on_login', 'auto_cleanup',
                               'debug_mode', 'low_impact'), lambda v: type(v) is bool)
    for key, minimum in RETENTION_LIMITS.items():
        _optional_fields(payload, (key,), lambda v: type(v) is int and minimum <= v <= MAX_RETENTION_DAYS)
    for key, maximum in HIGHLIGHT_LIMITS.items():
        _optional_fields(payload, (key,), lambda v: type(v) is int and 0 <= v <= maximum)
    for key, value in payload.items():
        if key.startswith('notify_') and key.endswith('_seconds') and (
                not _number(value) or not 1 <= value <= 60):
            raise _field_error()


def _validate_payload(table, row):
    if table == 'capture_jobs':
        # The image is the evidence. Recognition is run again locally.
        if (row.get('payload') is not None or row.get('signature') is not None
                or row['status'] not in QUEUE_STATES or row['kind'] not in QUEUE_KINDS
                or type(row['manual']) is not int or row['manual'] not in (0, 1)):
            raise _field_error()
        _optional_fields(row, ('error',), _text)
        return
    if 'payload' not in row:
        return
    payload = row['payload']
    if not isinstance(payload, dict):
        raise _field_error()
    if table == 'matches':
        _validate_match(payload, row)
    elif table == 'settings':
        _validate_settings(payload)
    elif table in ('lobby_rank', 'lobby_history', 'rp_checks'):
        _validate_rank(payload)
        if table == 'lobby_rank' and not _date(payload.get('captured_at')):
            raise _field_error()
    elif table == 'hud_backups':
        _validate_players(payload.get('players', []))
        _optional_fields(payload, PLAYER_COUNTS + ('live_rp',), _number)
        _optional_fields(payload, ('id', 'round_id'), _identifier)
        _optional_fields(payload, ('time',), _date)
        _optional_fields(payload, ('squad_path',), _text)
    elif table == 'profile_snapshots':
        _optional_fields(payload, ('name', 'mode_label', 'selected_season'), _text)
        for key in ('lifetime', 'season'):
            stats = payload.get(key, {})
            if not isinstance(stats, dict):
                raise _field_error()
            display = stats.get('display_values', {})
            approximated = stats.get('approximate_fields', [])
            if not isinstance(display, dict) or not all(_text(v) for v in display.values()):
                raise _field_error()
            if not isinstance(approximated, list) or not all(_text(v) for v in approximated):
                raise _field_error()
            if any(k not in ('display_values', 'approximate_fields') and v is not None
                   and not _number(v) for k, v in stats.items()):
                raise _field_error()


def _map_paths(table, row, transform):
    if 'path' in row:
        row['path'] = transform(row['path'])
    if table == 'matches':
        for item in row['payload']['evidence']:
            item['path'] = transform(item['path'])
    if table == 'hud_backups' and row['payload'].get('squad_path'):
        row['payload']['squad_path'] = transform(row['payload']['squad_path'])


def _digest_file(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def export_backup(store, path):
    path = Path(path)
    media = {}
    paths = {}

    def portable(value):
        try:
            source = resolve_media_path(store.root, value)
        except ValueError:
            return ''
        if source in paths:
            return paths[source]
        if not source.is_file():
            return '@missing/' + source.name
        if source.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError('原图过大，无法加入备份')
        name = f'media/{_digest_file(source)}{source.suffix.lower()}'
        media[name] = source
        paths[source] = '@' + name
        return paths[source]

    data = {}
    # Cleanup and capture cannot change referenced files during backup.
    with store.lock, HeroSamples._lock:
        for table in TABLES_V2:
            columns = [r[1] for r in store.db.execute(f'PRAGMA table_info("{table}")')]
            if not columns:
                continue
            rows = []
            if table == 'capture_jobs':
                raw_rows = store.db.execute("""SELECT j.* FROM capture_jobs j
                    WHERE j.status IN (?,?,?,?,?) AND (j.round_id IS NULL OR EXISTS (
                        SELECT 1 FROM capture_rounds r WHERE r.id=j.round_id
                        AND r.scope=j.scope AND r.discarded=0))""", QUEUE_STATES)
            else:
                raw_rows = store.db.execute(f'SELECT * FROM "{table}"')
            for raw in raw_rows:
                row = dict(zip(columns, raw))
                if table == 'capture_jobs':
                    row.update(payload=None, signature=None, error=None)
                for key in ('payload', 'scope'):
                    if isinstance(row.get(key), str):
                        try:
                            row[key] = json.loads(row[key])
                        except ValueError:
                            pass
                _map_paths(table, row, portable)
                if table == 'capture_jobs' and not row['path'].startswith('@media/'):
                    continue
                rows.append(row)
            data[table] = rows
        matches = len(data.get('matches', []))
        extras = export_extras(store.root, portable)
        extras_bytes = json.dumps(extras, ensure_ascii=False, allow_nan=False).encode('utf-8')
        records = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        manifest = json.dumps({
            'format': 'MatchLog', 'version': 2,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'matches': matches, 'images': len(media),
        }).encode('utf-8')
        if (len(records) > MAX_RECORDS_BYTES or len(extras_bytes) > MAX_EXTRAS_BYTES or len(media) + 3 > MAX_ENTRIES
                or len(records) + len(extras_bytes) + len(manifest)
                + sum(p.stat().st_size for p in media.values()) > MAX_EXPANDED_BYTES):
            raise ValueError('备份文件过大')
        with NamedTemporaryFile(dir=path.parent, suffix='.part', delete=False) as temporary:
            temp = Path(temporary.name)
        try:
            with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                archive.writestr('manifest.json', manifest)
                archive.writestr('records.json', records)
                archive.writestr('extras.json', extras_bytes)
                for name, source in media.items():
                    archive.write(source, name)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return {'matches': matches, 'images': len(media), 'bytes': path.stat().st_size,
            'capture_jobs': len(data.get('capture_jobs', [])), 'portrait_samples': len(extras['hero_samples']),
            'schedule': extras['season_schedule'] is not None}


def _archive_entries(archive):
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
        raise ValueError('备份文件过大')
    entries = {}
    for info in infos:
        name = info.filename
        if (info.orig_filename != name or name in entries or info.flag_bits & 1
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise ValueError('备份文件条目不正确')
        if name == 'manifest.json':
            limit = MAX_MANIFEST_BYTES
        elif name == 'records.json':
            limit = MAX_RECORDS_BYTES
        elif name == 'extras.json':
            limit = MAX_EXTRAS_BYTES
        elif MEDIA_NAME.fullmatch(name):
            limit = MAX_IMAGE_BYTES
        elif name == 'media/' and info.is_dir() and info.file_size == 0:
            limit = 0
        else:
            raise ValueError('备份路径不正确')
        if info.file_size > limit:
            raise ValueError('备份文件条目过大')
        entries[name] = info
    if not {'manifest.json', 'records.json'}.issubset(entries):
        raise ValueError('备份缺少必要文件')
    return entries


def _reject_constant(_):
    raise _field_error()


def _media_ref(value, entries):
    if not isinstance(value, str) or not value.startswith('@media/'):
        raise ValueError('备份图片路径不正确')
    name = value[1:]
    if name not in entries or not MEDIA_NAME.fullmatch(name):
        raise ValueError('备份缺少图片')
    return name


def _prepare_rows(store, data, entries, version):
    if not isinstance(data, dict) or data.keys() - (TABLES_V2 if version == 2 else TABLES):
        raise ValueError('备份结构不正确')
    prepared = {}
    referenced = {}

    def local(value):
        if not isinstance(value, str):
            raise _field_error()
        if value.startswith('@media/'):
            name = _media_ref(value, entries)
            basename = name.removeprefix('media/')
            path = resolve_media_path(store.root, media_dir(store.root,'restored') / basename)
            referenced[name] = path
            return str(path)
        elif not value or value.startswith('@missing/'):
            # Older exports use an empty string or a missing basename. Keep it
            # unavailable and derive a safe local marker instead of trusting it.
            tail = value.removeprefix('@missing/')
            if any(char in tail for char in '/\\:\x00') or tail in ('.', '..'):
                raise ValueError('备份图片路径不正确')
            suffix = Path(tail).suffix.lower()
            suffix = suffix if suffix in IMAGE_SUFFIXES else '.png'
            basename = f'missing-{hashlib.sha256(tail.encode()).hexdigest()}{suffix}'
        else:
            raise ValueError('备份图片路径不正确')
        return str(resolve_media_path(store.root, media_dir(store.root,'restored') / basename))

    with store.lock:
        for table, rows in data.items():
            if not isinstance(rows, list):
                raise ValueError('备份记录不正确')
            columns = {r[1] for r in store.db.execute(f'PRAGMA table_info("{table}")')}
            if not columns and rows:
                raise ValueError('应用版本不支持该备份')
            prepared[table] = []
            for row in rows:
                if (not isinstance(row, dict) or row.keys() - columns
                        or not TABLE_FIELDS[table].issubset(row)):
                    raise _field_error()
                if table == 'settings':
                    if type(row['id']) is not int or row['id'] != 1 or prepared[table]:
                        raise _field_error()
                else:
                    _optional_fields(row, ('id', 'match_id', 'round_id'), _identifier)
                    if any(row.get(key) is None for key in TABLE_FIELDS[table]):
                        raise _field_error()
                _optional_fields(row, ('season', 'split', 'kind', 'hash', 'phase', 'mode', 'game_key'), _text)
                _optional_fields(row, ('created', 'started', 'seen'),
                                 lambda v: _number(v) and -62_135_596_800 < v < 253_402_300_799)
                if table in ('capture_rounds', 'lobby_history', 'capture_jobs'):
                    scope = row['scope']
                    if not isinstance(scope, list) or len(scope) not in (3, 4) or not all(_text(v) and v for v in scope):
                        raise _field_error()
                    if len(scope) == 4:
                        if table == 'capture_jobs':
                            raise _field_error()
                        if table == 'capture_rounds':
                            row.setdefault('mode', scope[3])
                        row['scope'] = scope[:3]
                else:
                    _optional_fields(row, ('scope',), _text)
                _validate_payload(table, row)
                if table == 'capture_jobs':
                    name = _media_ref(row['path'], entries)
                    basename = 'portable-' + name.removeprefix('media/')
                    row['path'] = str(resolve_media_path(store.root, media_dir(store.root,'inbox') / basename))
                    if row['status'] == 'processing':
                        row['status'] = 'pending'
                else:
                    if table == 'hud_backups':
                        squad = row['payload'].get('squad_path')
                        if squad and not squad.startswith(('@media/', '@missing/')):
                            # Older exports copied the original machine's absolute path.
                            row['payload'].pop('squad_path')
                    _map_paths(table, row, local)
                for key in ('payload', 'scope'):
                    if isinstance(row.get(key), (dict, list)):
                        row[key] = json.dumps(row[key], ensure_ascii=False, allow_nan=False)
                if len((row.get('payload') or '').encode('utf-8')) > MAX_ROW_PAYLOAD_BYTES:
                    raise ValueError('备份中的单条记录过大')
                if any(value is not None and not _text(value) and not _number(value)
                       and not (key == 'payload' and isinstance(value, str))
                       for key, value in row.items()):
                    raise _field_error()
                prepared[table].append(row)
    return prepared, referenced


def _stage_image(archive, info, target):
    from PIL import Image

    digest = hashlib.sha256()
    count = 0
    with archive.open(info) as source, target.open('wb') as destination:
        while block := source.read(CHUNK_SIZE):
            count += len(block)
            if count > info.file_size or count > MAX_IMAGE_BYTES:
                raise ValueError('备份图片过大')
            digest.update(block)
            destination.write(block)
    if count != info.file_size or digest.hexdigest() != target.stem:
        raise ValueError('备份图片校验失败')
    try:
        with Image.open(target) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS or image.format not in {'PNG', 'JPEG', 'BMP', 'WEBP'}:
                raise ValueError('备份图片格式或尺寸不支持')
            image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError('备份图片格式不正确') from error


class _RestoreFiles:
    """Track only files created or intentionally replaced by this restore."""

    def __init__(self):
        self.installed = []
        self.folders = []
        self.replaced = []

    def _mkdir(self, folder):
        if not folder.exists():
            self._mkdir(folder.parent)
            folder.mkdir()
            self.folders.append(folder)

    def install(self, source, target):
        if target.exists():
            if (not target.is_file() or target.stat().st_size != source.stat().st_size
                    or _digest_file(target) != source.stem):
                raise ValueError('本地备份图片与校验值不符')
            return
        self._mkdir(target.parent)
        with target.open('xb') as destination:
            self.installed.append(target)
            with source.open('rb') as stream:
                shutil.copyfileobj(stream, destination, CHUNK_SIZE)

    def write_json(self, path, value):
        data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        previous = path.read_bytes() if path.exists() else None
        self._mkdir(path.parent)
        self._replace(path, data)
        if previous is None:
            self.installed.append(path)
        else:
            self.replaced.append((path, previous))

    @staticmethod
    def _replace(path, data):
        with NamedTemporaryFile(dir=path.parent, suffix='.part', delete=False) as stream:
            temporary = Path(stream.name)
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def rollback(self):
        for path, data in reversed(self.replaced):
            self._replace(path, data)
        for path in reversed(self.installed):
            path.unlink(missing_ok=True)
        for folder in reversed(self.folders):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()


def _insert_row(store, table, row):
    marks = ','.join('?' for _ in row)
    names = ','.join(f'"{column}"' for column in row)
    return store.db.execute(f'INSERT OR IGNORE INTO "{table}" ({names}) VALUES ({marks})',
                            tuple(row.values())).rowcount == 1


def _restore_jobs(store, prepared, staged, files):
    rounds = {row['id']: row for row in prepared.get('capture_rounds', [])}
    count = 0
    for job in prepared.get('capture_jobs', []):
        if (ident := job.get('round_id')):
            source = rounds.get(ident)
            expected = (job['scope'], source.get('match_id'), source.get('game_key')) if source else None
            actual = store.db.execute("""SELECT scope,match_id,game_key FROM capture_rounds
                WHERE id=? AND discarded=0""", (ident,)).fetchone()
            if not source or source.get('scope') != job['scope'] or source.get('discarded') or actual != expected:
                continue
            if actual[1]:
                season, split, account = json.loads(job['scope'])
                match = store.db.execute("""SELECT 1 FROM matches WHERE id=?
                    AND json_extract(payload,'$.season')=? AND json_extract(payload,'$.split')=?
                    AND json_extract(payload,'$.account')=? AND json_extract(payload,'$.deleted_at') IS NULL
                    AND json_extract(payload,'$.merged_into') IS NULL""", (actual[1], season, split, account)).fetchone()
                if not match:
                    continue
        if _insert_row(store, 'capture_jobs', job):
            target = resolve_media_path(store.root, job['path'])
            files.install(staged['media/' + target.name.removeprefix('portable-')], target)
            count += 1
    return count


def _commit_restore(store, prepared, staged, referenced, extras=None):
    from matchlog.migrations import backfill_unowned_matches

    with store.lock, HeroSamples._lock:
        before = store.db.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
        files = _RestoreFiles()
        imported_matches = set()
        extra_counts = {}
        try:
            with store.db:
                for table, rows in prepared.items():
                    if table in ('settings', 'capture_jobs'):
                        continue
                    for row in rows:
                        if _insert_row(store, table, row) and table == 'matches':
                            imported_matches.add(row['id'])
                if not before and prepared.get('settings') and (extras is None or not store.settings()['self_name']):
                    config = json.loads(prepared['settings'][0]['payload'])
                    config['start_on_login'] = False
                    store.db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)', (json.dumps(config, ensure_ascii=False),))
                if imported_matches:
                    backfill_unowned_matches(store, match_ids=imported_matches)
                after = store.db.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
                for name, target in referenced.items():
                    files.install(staged[name], resolve_media_path(store.root, target))
                if extras is not None:
                    jobs = _restore_jobs(store, prepared, staged, files)
                    extra_counts = {
                        **restore_extras(store.root, extras, staged, files),
                        'capture_jobs': jobs,
                        'capture_jobs_skipped': len(prepared.get('capture_jobs', [])) - jobs,
                    }
        except BaseException:
            files.rollback()
            raise
        store.active_id = None
    return {'imported': after - before, 'skipped': len(prepared.get('matches', [])) - (after - before),
            'images': len(staged), **extra_counts}


def import_backup(store, path):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = _archive_entries(archive)
            manifest = json.loads(archive.read(entries['manifest.json']), parse_constant=_reject_constant)
            if (not isinstance(manifest, dict) or manifest.get('format') != 'MatchLog'
                    or type(manifest.get('version')) is not int or manifest['version'] not in (1, 2)):
                raise ValueError('不支持这个备份版本')
            version = manifest['version']
            if (version == 2) != ('extras.json' in entries):
                raise ValueError('备份版本与文件结构不一致')
            data = json.loads(archive.read(entries['records.json']), parse_constant=_reject_constant)
            prepared, referenced = _prepare_rows(store, data, entries, version)
            extras = None
            needed = set(referenced)
            if version == 2:
                payload = json.loads(archive.read(entries['extras.json']), parse_constant=_reject_constant)
                extras = prepare_extras(payload, lambda value: _media_ref(value, entries))
                needed.update(name for _, name in extras[0].values())
                needed.update('media/' + Path(row['path']).name.removeprefix('portable-')
                              for row in prepared.get('capture_jobs', []))
            if not needed:
                return _commit_restore(store, prepared, {}, referenced, extras)
            with TemporaryDirectory(prefix='.restore-', dir=store.root) as directory:
                staged = {}
                for name in sorted(needed):
                    target = Path(directory) / name.removeprefix('media/')
                    _stage_image(archive, entries[name], target)
                    staged[name] = target
                return _commit_restore(store, prepared, staged, referenced, extras)
    except (zipfile.BadZipFile, NotImplementedError, RecursionError, UnicodeError) as error:
        raise ValueError('无法读取备份文件') from error
