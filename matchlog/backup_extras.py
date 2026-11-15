"""Portable snapshots of confirmed portrait learning and the season calendar."""

import hashlib
import json
import re

from matchlog.heroes import HeroSamples, LABELS, canonical_hero
from matchlog.media_paths import MEDIA_LAYOUT, existing_media_path, media_dir
from matchlog.season_calendar import DEFAULT, SeasonCalendar, aware_time

MAX_EXTRAS_BYTES = 2 * 1024 * 1024
MAX_PORTRAITS = 200
PORTRAIT_ID = re.compile(r'[a-f0-9]{20}')
HERO_NAMES = frozenset(LABELS.values())


def safe_extra_path(root, *parts):
    root = root.resolve()
    path = root.joinpath(*parts)
    if path.resolve() != path or path.is_symlink():
        raise ValueError('备份配置路径不安全')
    return path


def read_small_json(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_EXTRAS_BYTES + 1)
    if len(data) > MAX_EXTRAS_BYTES:
        raise ValueError('备份配置过大')
    def reject_constant(_):
        raise ValueError('备份配置字段不正确')

    return json.loads(data, parse_constant=reject_constant)


def schedule_snapshot(raw):
    if not isinstance(raw, dict) or type(raw.get('schema', 1)) is not int or raw.get('schema', 1) != 1:
        raise ValueError('赛季日程版本不支持')
    normalized = SeasonCalendar.validate({key: raw[key] for key in (*DEFAULT, 'last_applied') if key in raw})
    for key in ('verified_on', 'source', 'note'):
        if not isinstance(normalized[key], str) or len(normalized[key]) > 4096:
            raise ValueError('赛季日程字段不正确')
    current = normalized['current']
    if (not isinstance(current, dict) or not re.fullmatch(r'S\d{1,3}', str(current.get('season', '')))
            or str(current.get('split')) not in ('1', '2')):
        raise ValueError('赛季日程字段不正确')
    normalized['current'] = {'season': current['season'], 'split': str(current['split'])}
    if normalized.get('last_applied') is not None:
        normalized['last_applied'] = aware_time(normalized['last_applied']).isoformat()
    return normalized


def portrait_label(ident, row):
    if not PORTRAIT_ID.fullmatch(ident) or not isinstance(row, dict) or not isinstance(row.get('hero'), str):
        raise ValueError('英雄学习样本字段不正确')
    hero = canonical_hero(row['hero'])
    if hero not in HERO_NAMES:
        raise ValueError('英雄学习样本没有已知英雄标签')
    dates = {key: aware_time(row[key]).isoformat() for key in ('created_at', 'confirmed_at') if row.get(key)}
    if 'confirmed_at' not in dates:
        raise ValueError('英雄学习样本尚未确认')
    return {'hero': hero, **dates}


def portrait_signature(path, ident):
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.format != 'PNG' or image.size != (64, 64):
            raise ValueError('英雄学习样本尺寸不正确')
        crop = image.convert('RGB')
        if hashlib.sha256(crop.tobytes()).hexdigest()[:20] != ident:
            raise ValueError('英雄学习样本校验失败')
        gray = cv2.cvtColor(np.asarray(crop), cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, (16, 16)).astype(float)
    return ((small - small.mean()) / max(small.std(), 1)).round(2).ravel().tolist()


def export_extras(root, portable):
    root=root.resolve()
    folder=media_dir(root,'hero_samples')
    if not folder.is_dir() and (root/'hero_samples').is_dir():
        folder=root/'hero_samples'
    samples = HeroSamples(folder)
    portraits = {}
    try:
        local = read_small_json(safe_extra_path(root, *folder.relative_to(root.resolve()).parts, 'index.json'))
    except (ValueError, OSError):
        local = {}
    if isinstance(local, dict):
        for ident, row in local.items():
            try:
                label = portrait_label(ident, row)
                path = existing_media_path(root, samples.folder.absolute() / (ident + '.png'))
                if not path:
                    continue
                portrait_signature(path, ident)
                portraits[ident] = {**label, 'path': portable(path)}
            except (ValueError, OSError):
                continue
            if len(portraits) >= MAX_PORTRAITS:
                break
    try:
        schedule = schedule_snapshot(read_small_json(safe_extra_path(root, 'season_schedule.json')))
    except (ValueError, OSError, TypeError, KeyError):
        schedule = None
    return {'hero_samples': portraits, 'season_schedule': schedule}


def prepare_extras(raw, media_ref):
    if not isinstance(raw, dict) or raw.keys() - {'hero_samples', 'season_schedule'}:
        raise ValueError('备份配置字段不正确')
    portraits = raw.get('hero_samples', {})
    if not isinstance(portraits, dict) or len(portraits) > MAX_PORTRAITS:
        raise ValueError('英雄学习样本数量过多')
    samples = {}
    for ident, row in portraits.items():
        metadata = portrait_label(ident, row)
        samples[ident] = (metadata, media_ref(row.get('path')))
    schedule = raw.get('season_schedule')
    return samples, schedule_snapshot(schedule) if schedule is not None else None


def restore_extras(root, extras, staged, files):
    portraits, schedule = extras
    counts = {'portrait_samples': 0, 'portrait_samples_skipped': 0,
              'schedule': False, 'schedule_preserved': False}
    if schedule is not None:
        path = safe_extra_path(root, 'season_schedule.json')
        if path.exists():
            counts['schedule_preserved'] = True
        else:
            files.write_json(path, schedule)
            counts['schedule'] = True
    if not portraits:
        return counts
    index = safe_extra_path(root, *MEDIA_LAYOUT['hero_samples'], 'index.json')
    try:
        entries = read_small_json(index) if index.exists() else {}
    except (ValueError, OSError):
        entries = None
    if not isinstance(entries, dict):
        counts['portrait_samples_skipped'] = len(portraits)
        return counts
    changed = False
    for ident, (meta, name) in portraits.items():
        signature = portrait_signature(staged[name], ident)
        target = safe_extra_path(root, *MEDIA_LAYOUT['hero_samples'], ident + '.png')
        existing = entries.get(ident)
        if (ident not in entries and len(entries) >= MAX_PORTRAITS
                or existing is not None and (
                    not isinstance(existing, dict) or existing.get('hero') and existing['hero'] != meta['hero'])):
            counts['portrait_samples_skipped'] += 1
            continue
        present = target.is_file()
        if present:
            try:
                portrait_signature(target, ident)
            except (ValueError, OSError):
                counts['portrait_samples_skipped'] += 1
                continue
        else:
            files.install(staged[name], target)
            changed = True
        if not existing or not existing.get('hero'):
            entries[ident] = {**meta, 'signature': signature}
            changed = True
        elif present:
            counts['portrait_samples_skipped'] += 1
            continue
        counts['portrait_samples'] += 1
    if changed:
        files.write_json(index, entries)
    return counts
