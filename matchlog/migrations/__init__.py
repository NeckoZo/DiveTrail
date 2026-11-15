"""SQLite migrations for the local database."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3

from matchlog.recognition import nickname

SCHEMA_VERSION = 1


def account_assignments(db):
    """Use capture scope as provenance. A roster alone cannot identify its owner."""
    rounds = {}
    by_match = defaultdict(list)
    for ident, match_id, raw in db.execute('SELECT id,match_id,scope FROM capture_rounds'):
        try:
            season, split, name, *_ = json.loads(raw)
            owner = nickname(name)
        except (ValueError, TypeError):
            continue
        if not owner:
            continue
        scope = (season, split, owner)
        rounds[ident] = scope
        by_match[match_id].append(scope)

    for ident, raw in db.execute('SELECT id,payload FROM matches'):
        match = json.loads(raw)
        if match.get('account'):
            continue
        candidates = by_match.get(ident, []) + [rounds.get(match.get('round_id'))]
        owners = {owner for season, split, owner in filter(None, candidates)
                  if (season, split) == (match.get('season'), match.get('split'))}
        yield ident, match, next(iter(owners)) if len(owners) == 1 else None


def backfill_unowned_matches(store, match_ids=None):
    """May also run inside a portable restore transaction."""
    assigned = 0
    for ident, match, owner in list(account_assignments(store.db)):
        if owner and (match_ids is None or ident in match_ids):
            match['account'] = owner
            store.db.execute('UPDATE matches SET payload=? WHERE id=?', (json.dumps(match, ensure_ascii=False), ident))
            assigned += 1
    return assigned


def backup_database(store, label):
    folder = store.root / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%y%m%dT%H%M%S%fZ')
    path = folder / f'{label}-{stamp}.sqlite3'
    with closing(sqlite3.connect(path)) as copy:
        store.db.backup(copy)
    return path


def migrate(store):
    with store.lock:
        version = store.db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError('战绩库由较新版本创建，请使用兼容版本打开')
        if version == SCHEMA_VERSION:
            return
        if store.db.execute('SELECT 1 FROM matches LIMIT 1').fetchone():
            backup_database(store, 'before-account-migration')
        with store.db:
            backfill_unowned_matches(store)
            store.db.execute("CREATE INDEX IF NOT EXISTS matches_account_created ON matches(json_extract(payload,'$.account'), created)")
            store.db.execute('PRAGMA user_version = 1')
