"""Keep an independent, full-size preview for each qualifying summary."""

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
import time

from PIL import Image

from matchlog.capture_cleanup import DAY
from matchlog.config import DEFAULTS, HIGHLIGHT_LIMITS
from matchlog.image_storage import capture_name
from matchlog.media_paths import existing_media_path, media_dir, resolve_media_path
from matchlog.recognition import nickname


ENCODING = 'webp-q94'


def highlight_preview(store, match_id, kind):
    row = store.db.execute(
        "SELECT path FROM match_highlights WHERE match_id=? AND COALESCE(json_extract(payload,'$.kind'),'summary')=?",
        (match_id,kind),
    ).fetchone()
    return existing_media_path(store.root,row[0]) if row else None


def merged_highlight_previews(store, match):
    """Keep separate previews from each member of a manual merge, including chained merges."""
    if not match.get('preserve_merged_highlights'):
        return []
    rows = store.db.execute("""
        WITH RECURSIVE merged(id, payload) AS (
            SELECT id,payload FROM matches WHERE id=?
            UNION
            SELECT child.id,child.payload
            FROM merged parent, json_each(parent.payload,'$.merged_records') link
            JOIN matches child ON child.id=json_extract(link.value,'$.id')
            WHERE json_extract(child.payload,'$.merged_into')=parent.id
              AND json_extract(child.payload,'$.account')=?
              AND json_extract(child.payload,'$.season')=?
              AND json_extract(child.payload,'$.split')=?
        )
        SELECT m.id,COALESCE(json_extract(h.payload,'$.kind'),'summary'),
               json_extract(m.payload,'$.time'),h.path
        FROM merged m JOIN match_highlights h ON m.id=h.match_id
        ORDER BY m.id<>?, h.created, m.id
    """, (match['id'], match['account'], match['season'], match['split'], match['id']))
    previews = []
    for ident, kind, stamp, value in rows:
        if path := existing_media_path(store.root, value):
            previews.append((ident, kind, stamp, path))
    return previews


def _compact_highlight(original):
    with Image.open(io.BytesIO(original)) as image:
        source_is_webp = image.format == 'WEBP'
        with io.BytesIO() as encoded:
            image.convert('RGB').save(encoded, format='WEBP', quality=94, method=4)
            data = encoded.getvalue()
    return original if source_is_webp and len(original) <= len(data) else data


def _write_highlight(target, data):
    with NamedTemporaryFile(dir=target.parent, suffix='.part', delete=False) as file:
        temp = Path(file.name)
    try:
        temp.write_bytes(data)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def _remove_preview(path, folder):
    if path and (path.is_relative_to(folder) or path.parent==folder.parents[1]/'highlights'):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logging.warning('旧版高光副本暂时无法清理', exc_info=True)


def _retire_merged_previews(store, match, target, folder):
    for record in match.get('merged_records', []):
        ident = record.get('id')
        if not ident or ident == match['id']:
            continue
        row = store.db.execute("""
            SELECT h.path FROM match_highlights h JOIN matches m ON m.id=h.match_id
            WHERE h.match_id=? AND json_extract(m.payload,'$.merged_into')=?
            AND json_extract(m.payload,'$.account')=?
            """, (ident,match['id'],match.get('account'))).fetchone()
        if row:
            store.db.execute('DELETE FROM match_highlights WHERE match_id=?', (ident,))
            store.db.commit()
            path = existing_media_path(store.root, row[0])
            if path != target:
                _remove_preview(path, folder)


def _preview_path(store, folder, match_id, stamp, old):
    suffix = 1
    while True:
        name = capture_name('highlight', stamp, '.webp', suffix)
        target = resolve_media_path(store.root, folder / name)
        claimed = store.db.execute('SELECT 1 FROM match_highlights WHERE path=? AND match_id<>?',
                                   (str(target), match_id)).fetchone()
        if not claimed and (target == old or not target.exists()):
            return target
        suffix += 1


def sync_highlight(store, match):
    """Archive a qualifying settlement page at its original dimensions."""
    root = store.root.resolve()
    folder = media_dir(root,'highlights')
    with store.lock:
        if isinstance(match, str):
            row = store.db.execute('SELECT payload FROM matches WHERE id=?', (match,)).fetchone()
            match = json.loads(row[0]) if row else None
        if not match or match.get('deleted_at') or match.get('merged_into'):
            return False
        if match.get('preserve_merged_highlights'):
            # Each original keeps its own preview and its own retention clock.
            return bool(merged_highlight_previews(store, match))

        config = store.settings()
        stamp = datetime.fromisoformat(match['time']).astimezone(timezone(timedelta(hours=8)))
        if config['highlight_days'] and time.time() - stamp.timestamp() >= config['highlight_days'] * DAY:
            return False
        summary = match.get('summary') or {}
        players = summary.get('players') or []
        account = match.get('account') or nickname(config['self_name'])
        own = next((player for player in players if nickname(player.get('name', '')) == account), {})
        kills, damage, team = own.get('kills'), own.get('damage'), summary.get('squad_kills')
        known_kills = [player['kills'] for player in players if player.get('kills') is not None]
        known_team_kills = sum(known_kills)
        if team is None and len(known_kills) == len(players) == 3:
            team = known_team_kills
        team_floor = team if team is not None else known_team_kills
        snapshot = match.get('record_source') == 'last_snapshot'
        rank = match.get('rank') or {}
        ranked = match.get('mode') not in ('casual','event')
        rp = rank.get('delta') if ranked and (not rank.get('tally_pending') or match.get('rp_manual')) else None
        criteria = {key:config[key] for key in HIGHLIGHT_LIMITS}
        high_rp = bool(criteria['highlight_rp'] and rp is not None and rp >= criteria['highlight_rp'])
        high_damage = bool(criteria['highlight_damage'] and damage is not None and damage >= criteria['highlight_damage'])
        high_kills = bool(criteria['highlight_kills'] and kills is not None and kills >= criteria['highlight_kills'])
        high_team = bool(criteria['highlight_team_kills'] and team_floor >= criteria['highlight_team_kills'])
        qualifies = not snapshot and (high_kills or high_team or high_rp or high_damage)
        previous = store.db.execute(
            'SELECT path,hash,created,payload FROM match_highlights WHERE match_id=?', (match['id'],)
        ).fetchone()
        old = existing_media_path(root, previous[0]) if previous else None
        old_metadata = json.loads(previous[3]) if previous else {}
        if not qualifies:
            collected_with = old_metadata.get('criteria', {key:DEFAULTS[key] for key in HIGHLIGHT_LIMITS})
            needs_rp = old_metadata.get('rp') is not None and ranked and rp is None
            needs_damage = old_metadata.get('damage') is not None and damage is None
            if previous and (snapshot or (criteria == collected_with and kills is not None and team is not None
                                          and not needs_rp and not needs_damage)):
                store.db.execute('DELETE FROM match_highlights WHERE match_id=?', (match['id'],))
                store.db.commit()
                _remove_preview(old, folder)
                return False
            return bool(old)

        source = next((path for e in match.get('evidence', []) if e['kind'] == 'summary'
                       and (path := existing_media_path(root, e['path']))), None)
        kind = 'summary' if source else old_metadata.get('kind','summary')
        if source is None and high_rp and (old is None or kind == 'rank'):
            source = next((path for e in match.get('evidence', []) if e['kind'] == 'rank'
                           and (path := existing_media_path(root, e['path']))), None)
            if source is not None:
                kind = 'rank'
        if source is None and old is None:
            return False
        target = _preview_path(store, folder, match['id'], stamp, old)
        metadata = {'kills': kills, 'squad_kills': team, 'encoding': ENCODING, 'kind': kind, 'criteria': criteria}
        if high_rp:
            metadata['rp'] = rp
        if high_damage:
            metadata['damage'] = damage
        try:
            original = (source or old).read_bytes()
            digest = hashlib.sha256(original).hexdigest() if source or not previous else previous[1]
            current = old_metadata.get('encoding') == ENCODING
            if old == target and current and previous[1] == digest:
                if old_metadata != metadata:
                    store.db.execute('UPDATE match_highlights SET payload=? WHERE match_id=?',
                                     (json.dumps(metadata),match['id']))
                    store.db.commit()
                _retire_merged_previews(store, match, target, folder)
                return True
            folder.mkdir(parents=True,exist_ok=True)
            # With no original, a corrected name can reuse the existing compressed bytes.
            data = original if not source and current else _compact_highlight(original)
            _write_highlight(target, data)
        except (OSError, Image.DecompressionBombError):
            logging.warning('高光压缩失败，保留原有截图', exc_info=True)
            return bool(old)

        created = previous[2] if previous else time.time()
        store.db.execute('INSERT OR REPLACE INTO match_highlights VALUES (?,?,?,?,?)',
                         (match['id'], str(target), digest, created, json.dumps(metadata)))
        store.db.commit()
        if old != target and old != source:
            _remove_preview(old, folder)
        _retire_merged_previews(store, match, target, folder)
        return True
