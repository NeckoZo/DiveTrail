"""Aggregate hero usage from matches belonging to the current local profile."""

from math import isfinite
import unicodedata

from matchlog.heroes import LABELS, canonical_hero
from matchlog.recognition import display_name, nickname


FIELDS = ('damage', 'kills', 'assists', 'knocks')


def _hero_name(value):
    if not isinstance(value, str):
        return None
    label = unicodedata.normalize('NFKC', value).strip()
    if not label or label in ('—', '-', '未知', '待识别', '未识别'):
        return None
    return LABELS.get(label.casefold()) or canonical_hero(label)


def _valid_measurement(value):
    return type(value) in (int, float) and 0 <= value <= 100000 and isfinite(value)


def player_hero_stats(matches, config):
    """Return one player summary and per-hero samples for each tracked name.

    The caller supplies the account, season and split scope. A teammate is
    counted only when the summary also identifies the current player.
    """
    self_name = config.get('self_name') or ''
    self_key = nickname(self_name)
    players = {}
    for name in [self_name, *config.get('friends', [])]:
        key = nickname(name)
        if not key or key in players:
            continue
        players[key] = {
            'key': key, 'name': display_name(name), 'is_self': key == self_key,
            'games': 0, 'known_hero_games': 0, 'unknown_hero_games': 0,
            'wins': 0, 'placement_games': 0, 'heroes': {}, 'top_five': 0,
            'samples': {field: [] for field in FIELDS}, 'placements': [],
            'rp_games': 0, 'net_rp': 0, 'positive_rp': 0,
        }
    if self_key not in players:
        return []

    seen_matches = set()
    for match in matches:
        ident = match.get('id')
        if not ident or ident in seen_matches or match.get('merged_into'):
            continue
        seen_matches.add(ident)
        summary = match.get('summary') or {}
        participants = {}
        for participant in summary.get('players') or []:
            name = participant.get('name')
            key = nickname(name) if isinstance(name, str) else ''
            if key in players:
                participants.setdefault(key, participant)

        # A rank-only record still counts as a local game, with an unknown hero.
        # A partial summary without the owner is not proof a friend played with them.
        if self_key not in participants:
            participants = {self_key: {}}
        placement = summary.get('placement')
        has_placement = type(placement) is int and 1 <= placement <= 30
        win = has_placement and placement == 1
        rank = match.get('rank') or {}
        delta = rank.get('delta')
        for key, participant in participants.items():
            row = players[key]
            row['games'] += 1
            row['placement_games'] += has_placement
            row['wins'] += win
            if has_placement:
                row['placements'].append(placement)
                row['top_five'] += placement <= 5
            for field in FIELDS:
                value = participant.get(field)
                if _valid_measurement(value):
                    row['samples'][field].append(value)
            if (match.get('mode', 'ranked') == 'ranked' and type(delta) is int
                    and -3000 <= delta <= 3000):
                row['rp_games'] += 1
                row['net_rp'] += delta
                row['positive_rp'] += delta > 0
            hero = _hero_name(participant.get('hero'))
            if not hero:
                row['unknown_hero_games'] += 1
                continue
            row['known_hero_games'] += 1
            entry = row['heroes'].setdefault(hero, {
                'name': hero, 'games': 0, 'wins': 0, 'match_ids': [],
                'samples': {field: [0, 0] for field in (*FIELDS, 'placement')},
            })
            entry['games'] += 1
            entry['wins'] += win
            entry['match_ids'].append(ident)
            for field in (*FIELDS, 'placement'):
                value = placement if field == 'placement' else participant.get(field)
                valid = has_placement if field == 'placement' else _valid_measurement(value)
                if valid:
                    entry['samples'][field][0] += value
                    entry['samples'][field][1] += 1

    for row in players.values():
        measurements = row.pop('samples')
        placements = row.pop('placements')
        row['performance'] = {
            field: {'average': round(sum(values)/len(values), 1) if values else None,
                    'best': max(values, default=None), 'count': len(values)}
            for field, values in measurements.items()
        }
        row['average_placement'] = round(sum(placements)/len(placements), 1) if placements else None
        row['thousand_damage'] = sum(value >= 1000 for value in measurements['damage'])
        entries = row['heroes'].values()
        for entry in entries:
            for field, (total, count) in entry.pop('samples').items():
                average_key = 'average_placement' if field == 'placement' else field
                entry[average_key] = round(total / count, 1) if count else None
                entry[f'{field}_games'] = count
        row['heroes'] = sorted(entries, key=lambda entry: (-entry['games'], entry['name']))
    return list(players.values())
