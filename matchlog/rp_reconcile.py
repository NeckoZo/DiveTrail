"""Review in-game RP against checkpoints; rule estimates never become checkpoints."""

from bisect import bisect_right
from collections.abc import Mapping
from typing import Any, TypedDict

from matchlog.recognition import nickname

RULES = 'https://help.ea.com/en/articles/apex-legends/ranked/'
RULE_VERSION = 'S31.1-20261103'
VERIFIED_RULES = {('S31','1'):RULE_VERSION}
FEES = {'新秀': 0, '青铜': 10, '白银': 20, '黄金': 38, '白金': 48, '铂金': 48,
        '钻石': 65, '大师': 90, '猎杀者': 90}
PLACEMENT = [(125, 22), (100, 20), (75, 18), (55, 16), (45, 16), (40, 14),
             (20, 14), (20, 14), (10, 12), (10, 12), (5, 10), (5, 10),
             (5, 8), (5, 8), (5, 8), (0, 6)]


class RuleEstimate(TypedDict):
    value: float
    fee: int
    fee_source: str
    placement_rp: int
    combat_rp: float
    bonus: int
    rules: str
    rule_version: str


def rule_estimate(
    season: str,
    placement: int | None,
    kills: int | None,
    assists: int | None,
    participation: int | None,
    *,
    entry_cost: int | None = None,
    before_rank: str | None = None,
    bonus: int = 0,
    split: str = '1',
) -> RuleEstimate | None:
    version=VERIFIED_RULES.get((season,str(split)))
    if version is None or type(placement) is not int or not 1 <= placement <= 20:
        return None
    if type(kills) is not int or type(assists) is not int or type(participation) is not int:
        return None
    if not all(0 <= value <= 100 for value in (kills, assists, participation)):
        return None
    fee = entry_cost if entry_cost is not None else FEES.get((before_rank or '').split(' ')[0])
    if fee is None:
        return None
    place_rp, unit = PLACEMENT[min(placement, 16) - 1]
    kp = kills + assists + participation / 2
    combat = unit * (min(8, kp) + max(0, kp - 8) / 2)
    return {'value': place_rp + combat + bonus - fee, 'fee': fee, 'fee_source': 'screen' if entry_cost is not None else 'tier',
            'placement_rp': place_rp, 'combat_rp': combat, 'bonus': bonus, 'rules': RULES,'rule_version':version}


def breakdown_delta(rank: Mapping[str, Any]) -> int | None:
    fields = (rank.get('breakdown') or {})
    keys = ('combat_rp', 'bonus_rp', 'placement_rp', 'entry_cost')
    if all(type(fields.get(k)) is int and 0 <= fields[k] <= 3000 for k in keys):
        return fields['combat_rp'] + fields['bonus_rp'] + fields['placement_rp'] - fields['entry_cost']
    return None


def verify_rank(match, rank):
    """Cross-check the displayed net score when the season and counts are known."""
    version=VERIFIED_RULES.get((match.get('season'),str(match.get('split','1'))))
    details=rank.get('breakdown') or {}
    counts=rank.get('combat_counts') or {}
    value=rank.get('delta')
    tally=breakdown_delta(rank)
    fee=details.get('entry_cost')
    start_fee=match.get('entry_cost_at_start')
    if fee is None:fee=start_fee
    estimate=None
    if type(fee) is int and type(details.get('bonus_rp')) is int:
        estimate=rule_estimate(match['season'],rank.get('placement'),counts.get('kills'),counts.get('assists'),
                               counts.get('participation'),entry_cost=fee,bonus=details['bonus_rp'],
                               split=match.get('split','1'))
    rule_value=estimate['value'] if estimate else None
    if ((tally is not None and tally!=value) or (rule_value is not None and rule_value!=value)
            or (start_fee is not None and details.get('entry_cost') is not None and start_fee!=fee)):
        state='conflict'
    elif tally is not None or rule_value is not None:
        state='match'
    else:
        state='unavailable'
    return {'state':state,'rules':version,'tally':tally,'rule_value':rule_value,'entry_cost':fee,
            'entry_source':'settlement' if details.get('entry_cost') is not None else 'opening' if fee is not None else None}


def review(config, matches, checks):
    from matchlog.rp_progress import rank_from_total, rank_total, stamp
    season = config['season']
    own_name = nickname(config.get('self_name', ''))
    rows = []
    for m in matches:
        if m.get('mode', 'ranked') != 'ranked':
            continue
        rank = m.get('rank') or {}
        timed = not (m.get('record_origin') == 'upload' and not m.get('time_corrected'))
        at = stamp(m.get('rp_order_at'), stamp(m.get('rank_captured_at'), m['created']))
        raw = rank.get('delta')
        breakdown = breakdown_delta(rank)
        manual = m.get('rp_manual') or m.get('record_origin') == 'manual'
        if manual:
            breakdown = None
        pending = rank.get('tally_pending') and not manual
        rows.append({'match': m, 'at': at, 'timed': timed, 'absolute': rank_total(rank, season) if timed else None,
                     'value': None if pending else raw, 'raw': raw, 'breakdown': breakdown,
                     'source': 'pending' if pending else 'settlement', 'estimate': None})
    rows.sort(key=lambda row: row['at'])
    gaps = []
    anchors = [(r['at'], r['absolute'], i) for i, r in enumerate(rows) if r['absolute'] is not None]
    for c in checks:
        if c.get('source') != 'lobby' or (total := rank_total(c, season)) is None:
            continue
        at = stamp(c.get('captured_at'), stamp(c.get('created')))
        anchors.append((at, total, None))
    anchors.sort(key=lambda a: a[0])

    def set_value(row, value, source, before, after):
        row.update(value=value, source=source, before=before, after=after)

    times = [r['at'] for r in rows]
    last_unplaced = max((r['match']['created'] for r in rows if not r['timed'] and r['raw'] is not None), default=0)
    for (left_at, left, _), (right_at, right, _) in zip(anchors, anchors[1:]):
        has_unplaced = last_unplaced >= left_at
        between = [r for r in rows[bisect_right(times, left_at):bisect_right(times, right_at)] if r['timed']]
        if len(between) == 1 and not has_unplaced:
            r = between[0]
            rank = r['match'].get('rank') or {}
            details = rank.get('breakdown') or {}
            difference = right - left
            corroborated = r['breakdown'] == difference
            source = 'breakdown'
            if (not corroborated and rank.get('tally_pending') and not r['match'].get('rp_manual')
                    and details.get('entry_cost') is None
                    and all(type(details.get(k)) is int for k in ('combat_rp','bonus_rp','placement_rp'))):
                tier = rank_from_total(left, season).get('rank', '').split(' ')[0]
                fee = r['match'].get('entry_cost_at_start')
                if fee is None and (season,str(config.get('split','1'))) in VERIFIED_RULES:fee=FEES.get(tier)
                corroborated = fee is not None and sum(details[k] for k in ('combat_rp','bonus_rp','placement_rp')) - fee == difference
                source = 'anchors'
            if corroborated and r['value'] != difference:
                set_value(r, difference, source, left, right)
        missing = [r for r in between if r['value'] is None]
        known = sum(r['value'] for r in between if r['value'] is not None)
        remainder = right - left - known
        before = left
        for r in between:
            if before is None:
                break
            m = r['match']
            summary = m.get('summary') or {}
            rank = m.get('rank') or {}
            p = next((p for p in summary.get('players', []) if nickname(p.get('name', '')) == own_name), {})
            if r['value'] is None:
                details = rank.get('breakdown') or {}
                fee=details.get('entry_cost')
                r['estimate'] = rule_estimate(season, summary.get('placement'), p.get('kills'), p.get('assists'), p.get('participation'),
                                              entry_cost=fee if fee is not None else m.get('entry_cost_at_start'),
                                              bonus=details.get('bonus_rp') or 0,before_rank=rank_from_total(before, season).get('rank'),
                                              split=config.get('split','1'))
            before = before + r['value'] if r['value'] is not None else None
        if len(missing) == 1 and not has_unplaced:
            r = missing[0]
            # Agreement with the rules protects against attributing RP from an
            # unrecorded game to the visible one. A reviewed interval can also
            # establish coverage independently of the formula.
            if ((r['estimate'] and r['estimate']['value'] == remainder and not (r['match'].get('rank') or {}).get('tally_pending'))
                    or r['match'].get('rp_interval_verified') == [left, right]) and abs(remainder) <= 3000:
                set_value(r, remainder, 'anchors', left, right)
                remainder = 0
                missing = []
        if missing or remainder:
            gaps.append({'from_at': left_at, 'to_at': right_at, 'value': remainder,
                         'missing': len(missing), 'unattributed': True})
    # Offer a clearly marked estimate after the latest read if a capture was
    # missed; do not add it to the authoritative totals.
    if anchors:
        at, before, _ = anchors[-1]
        for r in (r for r in rows if r['timed'] and r['at'] > at):
            if r['value'] is None:
                s = r['match'].get('summary') or {}
                p = next((p for p in s.get('players', []) if nickname(p.get('name', '')) == own_name), {})
                r['estimate'] = rule_estimate(season, s.get('placement'), p.get('kills'), p.get('assists'), p.get('participation'),
                                              before_rank=rank_from_total(before, season).get('rank'),
                                              entry_cost=r['match'].get('entry_cost_at_start'),split=config.get('split','1'))
                break
            before += r['value']
    return rows, anchors, gaps


def apply_review(match, entry):
    if not entry:
        return match
    rank = dict(match.get('rank') or {})
    if entry['source'] != 'settlement':
        rank.update(delta=entry['value'], read_delta=entry['raw'], delta_source=entry['source'])
    if entry['estimate']:
        rank['rule_estimate'] = entry['estimate']
    return {**match, 'rank': rank} if rank else match


def window_net(rows, anchors, start, end):
    daily = [r for r in rows if r['timed'] and start <= r['match']['created'] <= end]
    known = [r['value'] for r in daily if r['value'] is not None]
    result = {'value': sum(known) if known else None, 'source': 'recorded', 'known': len(known),
              'missing': len(daily) - len(known), 'unattributed': None}
    if not daily:
        return result
    first, last = daily[0], daily[-1]
    before = next((a for a in reversed(anchors) if start <= a[0] < first['at']), None)
    after = next((a for a in reversed(anchors) if last['at'] <= a[0] <= end), None)
    opening = before[1] if before else first['absolute'] - first['value'] if first['absolute'] is not None and first['value'] is not None else None
    if opening is not None and after and all(start <= r['match']['created'] <= end for r in rows if first['at'] <= r['at'] <= after[0]):
        net = after[1] - opening
        result.update(value=net, source='lobby' if after[2] is None else 'settlement', unattributed=net - sum(known),
                      from_at=before[0] if before else first['at'], to_at=after[0])
    return result
