"""Explicit profile switches and transactional nickname migration."""

import json
import time
import uuid

from matchlog.migrations import backup_database
from matchlog.recognition import nickname
from matchlog.rp_progress import stamp


ROUND_TABLES = ('capture_rounds', 'capture_jobs', 'lobby_history')
ALIAS_PREFIX = 'profile-alias:'
ALIAS_FIELD = '_previous_owners'
CONFLICT = '两个昵称的历史记录存在冲突。请选择「建立新档案」分开保留。'


def _key(value):
    return nickname(value) if isinstance(value, str) else ''


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _scope(value, *, delimited=False):
    if not isinstance(value, str):
        return None
    try:
        fields = value.split('|', 2) if delimited else json.loads(value)
    except ValueError:
        return None
    return fields if isinstance(fields, list) and len(fields) >= 3 else None


def _move_name(payload, old, new_name):
    for field in ('name', 'account'):
        if _key(payload.get(field)) == old:
            payload[field] = new_name if field == 'name' else nickname(new_name)


def _move_roster(payload, old, new_name):
    players = payload.get('players') or []
    names = [_key(p.get('name')) for p in players]
    if old not in names:
        return
    if nickname(new_name) in names or names.count(old) > 1:
        raise ValueError(CONFLICT)
    players[names.index(old)]['name'] = new_name


def _history(payload, old, cutoff):
    aliases = payload.get(ALIAS_FIELD)
    aliases = dict(aliases) if isinstance(aliases, dict) else {}
    aliases[old] = cutoff
    return aliases


def _round_aliases(db, round_id):
    row = db.execute('SELECT value FROM recorder_flags WHERE name=?', (ALIAS_PREFIX+round_id,)).fetchone()
    try:
        aliases = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        return {}
    return aliases if isinstance(aliases, dict) else {}


def _archive_initial_rp(db, config, account):
    """Retain a manual starting score when selecting another local profile."""
    if account and config.get('initial_rp') is not None:
        scope = '|'.join((config['season'], config['split'], account))
        data = {'total_rp': config['initial_rp'], 'source': 'initial', 'label': '开始记录'}
        db.execute('INSERT INTO rp_checks VALUES (?,?,?,?)',
                   (uuid.uuid4().hex, scope, stamp(config.get('initial_rp_at')), _json(data)))


def _migrate_profile(db, old, new, new_name):
    cutoff = time.time()
    records = {ident: json.loads(payload) for ident, payload in db.execute('SELECT id,payload FROM matches')}
    rounds = {ident: _scope(raw) for ident, raw in db.execute('SELECT id,scope FROM capture_rounds')}
    owners = {ident: _key(fields[2]) for ident, fields in rounds.items() if fields}
    old_rounds = {ident for ident, owner in owners.items() if owner == old}
    old_matches = {ident for ident, match in records.items() if _key(match.get('account')) == old}

    # An existing destination may belong to another person who happens to use the
    # same nickname. Switching is safe and leaves both histories separate.
    if any(_key(match.get('account')) == new for match in records.values()) or new in owners.values():
        raise ValueError(CONFLICT)
    for table in ('capture_jobs', 'lobby_history', 'lobby_rank', 'rp_checks'):
        for raw, in db.execute(f'SELECT scope FROM {table}'):
            fields = _scope(raw, delimited=table in ('lobby_rank', 'rp_checks'))
            if fields and _key(fields[2]) == new:
                raise ValueError(CONFLICT)
    if db.execute('SELECT 1 FROM profile_snapshots WHERE scope=? LIMIT 1', (new,)).fetchone():
        raise ValueError(CONFLICT)
    if db.execute("SELECT 1 FROM hud_backups WHERE json_extract(payload,'$.account')=? LIMIT 1", (new,)).fetchone():
        raise ValueError(CONFLICT)

    for ident in old_matches:
        match = records[ident]
        linked = match.get('round_id')
        if linked in owners and linked not in old_rounds:
            raise ValueError(CONFLICT)
        match['account'] = new
        match[ALIAS_FIELD] = _history(match, old, cutoff)
        if match.get('summary'):
            _move_roster(match['summary'], old, new_name)
        db.execute('UPDATE matches SET payload=? WHERE id=?', (_json(match), ident))

    for table in ROUND_TABLES:
        columns = 'id,scope,match_id' if table == 'capture_rounds' else 'id,scope,round_id'
        rows = db.execute(f'SELECT {columns} FROM {table}').fetchall()
        for ident, raw, linked in rows:
            fields = _scope(raw)
            if not fields or _key(fields[2]) != old:
                continue
            if table == 'capture_rounds':
                if linked in records and linked not in old_matches:
                    raise ValueError(CONFLICT)
                aliases = _round_aliases(db, ident)
                aliases[old] = cutoff
                db.execute('INSERT OR REPLACE INTO recorder_flags VALUES (?,?)',
                           (ALIAS_PREFIX+ident, _json(aliases)))
            elif linked in owners and linked not in old_rounds:
                raise ValueError(CONFLICT)
            fields[2] = new
            db.execute(f'UPDATE {table} SET scope=? WHERE id=?', (_json(fields), ident))
            if table == 'capture_jobs':
                _migrate_observation(db, ident, old, new_name)

    for ident, raw, match_id in db.execute('SELECT id,payload,match_id FROM hud_backups').fetchall():
        payload = json.loads(raw)
        owner = _key(payload.get('account'))
        if owner != old and (owner or
                (payload.get('round_id') not in old_rounds and match_id not in old_matches)):
            continue
        if match_id in records and match_id not in old_matches:
            raise ValueError(CONFLICT)
        payload['account'] = new
        _move_roster(payload, old, new_name)
        db.execute('UPDATE hud_backups SET payload=? WHERE id=?', (_json(payload), ident))

    for table in ('lobby_rank', 'rp_checks'):
        for rowid, raw, data in db.execute(f'SELECT rowid,scope,payload FROM {table}').fetchall():
            fields = _scope(raw, delimited=True)
            if not fields or _key(fields[2]) != old:
                continue
            fields[2] = new
            payload = json.loads(data)
            _move_name(payload, old, new_name)
            db.execute(f'UPDATE {table} SET scope=?,payload=? WHERE rowid=?',
                       ('|'.join(fields), _json(payload), rowid))
    for ident, raw in db.execute('SELECT id,payload FROM profile_snapshots WHERE scope=?', (old,)).fetchall():
        payload = json.loads(raw)
        _move_name(payload, old, new_name)
        payload[ALIAS_FIELD] = _history(payload, old, cutoff)
        db.execute('UPDATE profile_snapshots SET scope=?,payload=? WHERE id=?', (new, _json(payload), ident))
    return len(old_matches)


def _migrate_observation(db, ident, old, new_name):
    raw, signature = db.execute('SELECT payload,signature FROM capture_jobs WHERE id=?', (ident,)).fetchone()
    if not raw:
        return
    payload = json.loads(raw)
    fields = {key: value for key, value in payload.items() if key != '_origin_round_id'}
    old_signature = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    _move_name(payload, old, new_name)
    _move_roster(payload, old, new_name)
    if signature == old_signature:
        fields = {key: value for key, value in payload.items() if key != '_origin_round_id'}
        signature = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    db.execute('UPDATE capture_jobs SET payload=?,signature=? WHERE id=?', (_json(payload), signature, ident))


def rename_profile(store, new_name, merge=False, expected_name=None):
    """Activate a nickname, optionally migrating the old account after a backup."""
    name = new_name.strip()[:64] if isinstance(new_name, str) else ''
    new = _key(name)
    if not new:
        raise ValueError('请填写 Apex 游戏内昵称')
    with store.lock:
        if store.db.in_transaction:
            raise RuntimeError('档案正在写入，请稍后再试')
        version = store.db.execute('PRAGMA data_version').fetchone()[0]
        config = store.settings()
        old = _key(config['self_name'])
        if expected_name is not None and _key(expected_name) != old:
            raise ValueError('当前档案已变更，请重新确认昵称')
        backup = backup_database(store, 'before-profile-merge') if merge and old and old != new else None
        matches = 0
        with store.db:
            store.db.execute('BEGIN IMMEDIATE')
            if store.db.execute('PRAGMA data_version').fetchone()[0] != version:
                raise ValueError('档案刚在另一窗口发生变更，请重新确认昵称')
            if old != new:
                if backup is not None:
                    matches = _migrate_profile(store.db, old, new, name)
                else:
                    _archive_initial_rp(store.db, config, old)
                    config.update(initial_rp=None, initial_rp_at=None)
                config.update(detected_mode='unknown', detected_mode_at=0)
            config['self_name'] = name
            store.db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)', (_json(config),))
        if old != new:
            store.active_id = None
            store.phase = None
            store.hud = None
            store.last_ingest = 0
        return {'config': config, 'backup': backup, 'matches': matches}


def relabel_historical_result(store, result, round_id, captured_at=None):
    """Recognize a previous self name only in its verified historical round."""
    if not round_id:
        return result
    with store.lock:
        row = store.db.execute(
            'SELECT started,match_id,phase FROM capture_rounds WHERE id=? AND scope=? AND discarded=0',
            (round_id, store.journal.scope()),
        ).fetchone()
        if not row:
            return result
        started, match_id, phase = row
        config = store.settings()
        name = config['self_name']
        current = _key(name)
        aliases = _round_aliases(store.db, round_id)
        if match_id:
            record = store.db.execute(
                "SELECT created,json_extract(payload,'$._previous_owners') FROM matches "
                "WHERE id=? AND json_extract(payload,'$.account')=?",
                (match_id, current),
            ).fetchone()
            stored = json.loads(record[1]) if record and record[1] else {}
            if isinstance(stored, dict):
                aliases = {**stored, **aliases}
                if phase == 'upload' and stored and captured_at is not None:
                    # Attaching an old screenshot to a manually entered match may
                    # create the round after the verified nickname change.
                    started = record[0]
        at = stamp(captured_at, started)
        eligible = {alias for alias, until in aliases.items()
                    if alias != current and type(until) in (int, float) and started <= until and at <= until}
        if not eligible:
            return result
        if result.get('kind') == 'profile' and _key(result.get('name')) in eligible:
            return {**result, 'name': name}
        players = result.get('players') or []
        keys = [_key(player.get('name')) for player in players]
        matches = [i for i, key in enumerate(keys)
                   if key in eligible and (result.get('kind') != 'hud' or players[i].get('slot') == 2)]
        if len(matches) != 1 or current in keys:
            return result
        renamed = list(players)
        renamed[matches[0]] = {**renamed[matches[0]], 'name': name}
        return {**result, 'players': renamed}


def profile_name_matches(result, archived, captured_at, current):
    """Allow an old nickname only when rereading its own stored profile image."""
    name = _key(result.get('name'))
    if not name:
        return False
    if name == current:
        return True
    aliases = archived.get(ALIAS_FIELD)
    cutoff = aliases.get(name) if isinstance(aliases, dict) else None
    return _key(archived.get('name')) == current and type(cutoff) in (int, float) and captured_at <= cutoff
