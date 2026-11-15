"""Expose the last plausible, same-round personal HUD score as evidence."""

import json

from matchlog.recognition import nickname
from matchlog.rp_progress import stamp


def plausible_hud_rp(value, entry_cost):
    floor = -entry_cost if type(entry_cost) is int and 0 <= entry_cost <= 120 else -120
    return type(value) is int and floor <= value <= 3000


def hud_score_evidence(store, matches, config):
    owner = nickname(config['self_name'])
    if not owner:
        return {}
    targets = {match['id']: match for match in matches if match.get('mode') == 'ranked'}
    if not targets:
        return {}
    scope = json.dumps([config['season'], config['split'], owner], ensure_ascii=False)
    with store.lock:
        rows = store.db.execute("""
            SELECT h.created,h.payload,r.match_id,r.game_key
            FROM hud_backups h JOIN capture_rounds r ON r.id=json_extract(h.payload,'$.round_id')
            WHERE h.season=? AND h.split=? AND r.scope=? AND r.discarded=0
              AND json_extract(h.payload,'$.identity')='self' AND r.match_id IS NOT NULL
              AND json_type(h.payload,'$.live_rp')='integer'
            ORDER BY h.created DESC
        """, (config['season'], config['split'], scope)).fetchall()
    evidence = {}
    for captured, raw, ident, key in rows:
        if ident in evidence or ident not in targets:
            continue
        match = targets[ident]
        hud = json.loads(raw)
        value = hud['live_rp']
        if not plausible_hud_rp(value, match.get('entry_cost_at_start')) or hud.get('account') not in (None, owner):
            continue
        if key and hud.get('game_key') not in (None, key):
            continue
        end = stamp(match.get('round_end_at'), stamp(match.get('settled_at'), None))
        if end is not None and captured > end + 3:
            continue
        evidence[ident] = {'value': value, 'at': captured}
    for ident, match in targets.items():
        value = match.get('snapshot_live_rp')
        if (ident not in evidence and match.get('snapshot_rp_verified')
                and plausible_hud_rp(value, match.get('entry_cost_at_start'))):
            evidence[ident] = {'value':value, 'at':match.get('snapshot_at')}
    return evidence
