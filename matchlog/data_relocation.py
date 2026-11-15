"""Rebase managed evidence paths after moving the whole data directory."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from matchlog.media_paths import IMAGE_FOLDERS, IMAGE_SUFFIXES, resolve_media_path


PATH_FIELDS = ('path', 'image_path', 'squad_path')
EVIDENCE_TABLES = (
    'matches', 'evidence', 'capture_jobs', 'hud_backups', 'lobby_rank',
    'lobby_history', 'rp_checks', 'profile_snapshots', 'match_highlights',
    'media_asset_paths',
)


def rebase_database(data_root, old_root):
    data_root, old_root = Path(data_root).resolve(), Path(old_root).absolute()
    source = data_root / 'matches.sqlite3'
    if not source.is_file() or old_root == data_root:
        return 0

    def rewrite_path(value):
        if not isinstance(value, str):
            return value
        path = Path(value)
        if not path.is_absolute() or not path.is_relative_to(old_root):
            return value
        relative = path.relative_to(old_root)
        if relative.parts[:-1] not in IMAGE_FOLDERS or relative.suffix.lower() not in IMAGE_SUFFIXES:
            return value
        return str(resolve_media_path(data_root, data_root / relative))

    def rewrite_payload(value):
        if isinstance(value, list):
            return [rewrite_payload(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite_path(item) if key in PATH_FIELDS else rewrite_payload(item)
                    for key, item in value.items()}
        return value

    def rewrite_json(raw):
        if not isinstance(raw, str):
            return raw
        value = json.loads(raw)
        updated = rewrite_payload(value)
        return json.dumps(updated, ensure_ascii=False) if updated != value else raw

    with closing(sqlite3.connect(source)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        updates = 0
        for table in EVIDENCE_TABLES:
            if table not in existing:
                continue
            columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            for column, convert in (('path', rewrite_path), ('payload', rewrite_json)):
                if column not in columns:
                    continue
                rows = [(new, row_id) for row_id, raw in db.execute(f'SELECT rowid, {column} FROM {table}')
                        if (new := convert(raw)) != raw]
                db.executemany(f'UPDATE {table} SET {column}=? WHERE rowid=?', rows)
                updates += len(rows)
        if 'recorder_flags' in existing:
            from matchlog.record_merge import UNDO
            row = db.execute('SELECT value FROM recorder_flags WHERE name=?', (UNDO,)).fetchone()
            if row:
                payload = json.loads(row[0])
                for group in ('before', 'after'):
                    for snapshot in payload.get(group, {}).values():
                        snapshot[1] = rewrite_json(snapshot[1])
                db.execute('UPDATE recorder_flags SET value=? WHERE name=?',
                           (json.dumps(payload, ensure_ascii=False), UNDO))
        return updates
