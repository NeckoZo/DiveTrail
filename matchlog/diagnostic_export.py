"""Share a bounded capture report without screenshots or identifying strings."""

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
import time
import unicodedata

from PIL import Image

from matchlog.capture_diagnostics import CHINA, PHASES
from matchlog.capture_trace import OUTCOMES, REASONS, ROUTES
from matchlog.completeness import record_status
from matchlog.media_paths import resolve_media_path
from matchlog.recognition import display_name, nickname


WINDOW_HOURS = 24
ROW_LIMIT = 300
JOB_LIMIT = 1200
STATUSES = ('pending', 'processing', 'deferred', 'review', 'failed', 'done', 'archived', 'dismissed')
MODES = ('ranked', 'casual', 'event', 'unknown')
RANK_SOURCES = ('settlement', 'pending', 'anchors', 'breakdown', 'manual', 'lobby')


def report_filename(player, stamp, *, single_match=False):
    """Identify the export in its filename while keeping the report anonymous."""
    label='matchlog-match-log' if single_match else 'matchlog-diagnostics'
    name=''.join(c for c in display_name(player) if unicodedata.category(c)[0]!='C')
    name=re.sub(r'[<>:"/\\|?*]+','-',name)[:40].strip(' .-')
    prefix=f'{name}-' if name else ''
    return f'{prefix}{label}-{stamp}.json'


def _option(value, allowed):
    return value if type(value) is str and value in allowed else None


def _number(value, lower, upper):
    return value if type(value) in (int, float) and lower <= value <= upper else None


def _time(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value).timestamp()
        return datetime.fromtimestamp(value, CHINA).isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _size(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        if all(type(n) is int and 0 < n <= 32768 for n in value):
            return list(value)
    return None


def _environment(display):
    display = display if isinstance(display, dict) else {}
    result = {'webview': {'device_pixel_ratio': _number(display.get('device_pixel_ratio'), .25, 10),
                          'viewport_css': _size(display.get('viewport_css')),
                          'screen_css': _size(display.get('screen_css'))}}
    if sys.platform == 'win32':
        version = sys.getwindowsversion()
        result['windows'] = {'major': version.major, 'minor': version.minor, 'build': version.build}
        result['monitors_px'] = []
        try:
            import mss
            from mss.exception import ScreenShotError
        except ImportError:
            return result
        try:
            with mss.mss() as capture:
                result['monitors_px'] = [[m['width'], m['height']] for m in capture.monitors[1:]]
        except (OSError, RuntimeError, ScreenShotError):
            pass
    return result


def _error_category(value):
    # Stored exceptions can contain raw filenames. Export only fixed categories.
    if not isinstance(value, str) or not value:
        return None
    if 'TextRecOutput' in value and 'boxes' in value:
        return 'ocr_output_shape'
    if '后台繁忙' in value:
        return 'backlog'
    if '赛季' in value or '赛段' in value:
        return 'scope'
    if '单帧' in value or '未读全' in value:
        return 'incomplete_read'
    if '截图已清理' in value or '原图已不存在' in value:
        return 'source_unavailable'
    return 'other'


def _rank(value):
    breakdown=value.get('breakdown') or {}
    validation=value.get('validation') or {}
    return {
        'delta': _number(value.get('delta'), -3000, 3000),
        'read_delta': _number(value.get('read_delta'), -3000, 3000),
        'total_rp': _number(value.get('total_rp'), 0, 2_000_000),
        'progress': _number(value.get('progress'), 0, 2_000_000),
        'capacity': _number(value.get('capacity'), 0, 2_000_000),
        'placement': _number(value.get('placement'), 1, 100),
        'source': _option(value.get('delta_source') or value.get('source'), RANK_SOURCES),
        'pending': bool(value.get('tally_pending')),
        'breakdown':{k:_number(breakdown.get(k),0,3000) for k in
                     ('combat_rp','bonus_rp','placement_rp','entry_cost','protection_rp')},
        'verification':_option(validation.get('state'),('match','conflict','unavailable')),
        'rule_version':_option(validation.get('rules'),('S31.1-20261103',)),
    }


def _file_metadata(path):
    try:
        stat = path.stat()
        if not path.is_file():
            return {'present': False}
    except OSError:
        return {'present': False}
    result = {'present': True, 'bytes': stat.st_size, 'modified_at': _time(stat.st_mtime)}
    try:
        with Image.open(path) as image:
            result['pixels'] = _size(image.size)
            result['format'] = _option(image.format, ('PNG', 'JPEG', 'WEBP', 'BMP'))
    except (OSError, ValueError, Image.DecompressionBombError):
        result['pixels'] = None
    return result


def build_report(store, diagnostics, runtime, display=None, *, version=None, running=False, portable=False, now=None, match_id=None):
    """Build the report from allowlisted fields; never serialize a full payload."""
    now = time.time() if now is None else now
    since = now - WINDOW_HOURS * 3600
    bounds = (since, now)
    # Stored previous-run event aliases cannot be joined to a new runtime. The
    # database trace below carries safe per-job history across app restarts.
    events, aliases = diagnostics.snapshot(max(since,diagnostics.started_at))
    next_alias = max([*aliases.values(), *(value for row in events for value in row[3:] if value is not None)], default=0)
    paths = {}

    def alias(category, value):
        nonlocal next_alias
        if not value:
            return None
        key = (category, str(value))
        if key not in aliases:
            next_alias += 1
            aliases[key] = next_alias
        return aliases[key]

    def file_ref(value, kind):
        try:
            path = resolve_media_path(store.root, value)
        except ValueError:
            return None
        if path not in paths:
            paths[path] = {'file': len(paths)+1, 'kind': kind}
        return paths[path]['file']

    with store.lock:
        scope = store.journal.scope()
        config = store.settings()
        account = nickname(config['self_name'])
        trace_rows={}
        selected_rounds=set()
        if match_id is not None:
            row=store.db.execute("SELECT created,payload FROM matches WHERE id=? AND json_extract(payload,'$.account')=?",
                                 (str(match_id),account)).fetchone()
            if not row or not account:raise ValueError('对局不存在')
            payload=json.loads(row[1])
            if payload.get('deleted_at') or payload.get('merged_into'):raise ValueError('对局不存在')
            selected_rounds={r[0] for r in store.db.execute('SELECT id FROM capture_rounds WHERE match_id=?',(match_id,))}
            if payload.get('round_id'):selected_rounds.add(payload['round_id'])
        raw_jobs = store.db.execute("""
            SELECT id,round_id,created,kind,path,status,manual,payload,error FROM capture_jobs
            WHERE scope=? AND created BETWEEN ? AND ? ORDER BY created DESC LIMIT ?
            """, (scope, *bounds, JOB_LIMIT + 1)).fetchall()
        raw_rounds = store.db.execute("""
            SELECT id,started,seen,phase,match_id,discarded,mode,game_key FROM capture_rounds
            WHERE scope=? AND seen BETWEEN ? AND ? ORDER BY seen DESC LIMIT ?
            """, (scope, *bounds, ROW_LIMIT + 1)).fetchall()
        raw_matches = store.db.execute("""
            SELECT id,created,payload FROM matches
            WHERE json_extract(payload,'$.account')=? AND created BETWEEN ? AND ?
            ORDER BY created DESC LIMIT ?
            """, (account, *bounds, ROW_LIMIT + 1)).fetchall() if account else []
        raw_hud = store.db.execute("""
            SELECT created,payload,path,match_id FROM hud_backups
            WHERE json_extract(payload,'$.account')=? AND created BETWEEN ? AND ?
            ORDER BY created DESC LIMIT ?
            """, (account, *bounds, ROW_LIMIT + 1)).fetchall() if account else []
        raw_lobby = store.db.execute("""
            SELECT created,payload,path,round_id FROM lobby_history
            WHERE scope=? AND created BETWEEN ? AND ? ORDER BY created DESC LIMIT ?
            """, (scope, *bounds, ROW_LIMIT + 1)).fetchall()
        other_scope_jobs = store.db.execute(
            'SELECT COUNT(*) FROM capture_jobs WHERE scope<>? AND created BETWEEN ? AND ?',
            (scope, *bounds),
        ).fetchone()[0]
        progress = store.rank_progress()
        if match_id is not None:
            round_filter=json.dumps(sorted(selected_rounds))
            raw_matches=[(str(match_id),*row)]
            raw_rounds=store.db.execute("""SELECT id,started,seen,phase,match_id,discarded,mode,game_key
                FROM capture_rounds WHERE id IN (SELECT value FROM json_each(?)) ORDER BY seen DESC LIMIT ?""",
                (round_filter,ROW_LIMIT+1)).fetchall()
            raw_jobs=store.db.execute("""SELECT j.id,j.round_id,j.created,j.kind,j.path,j.status,j.manual,j.payload,j.error
                FROM capture_jobs j LEFT JOIN capture_trace t ON t.job_id=j.id WHERE EXISTS (
                  SELECT 1 FROM json_each(?) WHERE value IN (j.round_id,t.round_id,t.origin_round_id,
                    json_extract(j.payload,'$._origin_round_id'))) ORDER BY j.created DESC LIMIT ?""",
                (round_filter,JOB_LIMIT+1)).fetchall()
            raw_hud=store.db.execute("""SELECT created,payload,path,match_id FROM hud_backups
                WHERE match_id=? OR json_extract(payload,'$.round_id') IN (SELECT value FROM json_each(?))
                ORDER BY created DESC LIMIT ?""",(match_id,round_filter,ROW_LIMIT+1)).fetchall()
            raw_lobby=store.db.execute("""SELECT created,payload,path,round_id FROM lobby_history
                WHERE round_id IN (SELECT value FROM json_each(?)) ORDER BY created DESC LIMIT ?""",
                (round_filter,ROW_LIMIT+1)).fetchall()
            other_scope_jobs=0
            selected_aliases={aliases.get(('round',value)) for value in selected_rounds}-{None}
            selected_jobs={aliases.get(('job',item[0])) for item in raw_jobs}-{None}
            events=tuple(event for event in events if event[3] in selected_jobs or event[4] in selected_aliases)
        job_keys=json.dumps([item[0] for item in raw_jobs[:JOB_LIMIT]])
        for ident,rid,origin,finished,raw in store.db.execute("""SELECT * FROM capture_trace
                WHERE job_id IN (SELECT value FROM json_each(?))""",(job_keys,)):
            trace_rows[ident]=(rid,origin,finished,json.loads(raw))

    jobs = []
    for ident, rid, at, kind, path, status, manual, payload, error in raw_jobs[:JOB_LIMIT]:
        observed = json.loads(payload) if payload else {}
        jobs.append({
            'job': alias('job', ident), 'round': alias('round', rid),
            'original_round': alias('round', observed.get('_origin_round_id')),
            'game': alias('game', observed.get('game_key')),
            'at': _time(at), 'kind': _option(kind, PHASES), 'status': _option(status, STATUSES),
            'manual': bool(manual), 'recognized_as': _option(observed.get('kind'), PHASES),
            'error_category': _error_category(error),
            'file': file_ref(path, 'hud' if observed.get('_hud_managed') else 'queue'),
        })
        if ident in trace_rows:
            target,origin,finished,trace=trace_rows[ident]
            jobs[-1]['trace']={
                'round':alias('round',target),'original_round':alias('round',origin),
                'finished_at':_time(finished),'page':_option(trace.get('page'),PHASES),
                'outcome':_option(trace.get('outcome'),OUTCOMES),'reason':_option(trace.get('reason'),REASONS),
                'route':_option(trace.get('route'),ROUTES),'input_px':_size(trace.get('input_px')),
                'client_px':_size(trace.get('client_px')),'game_priority':bool(trace.get('game_priority')),
                **{key:_number(trace.get(key),0,7*24*3600*1000) for key in ('queued_ms','ocr_ms','process_ms','queue_depth')},
            }

    matches = []
    for ident, at, payload in raw_matches[:ROW_LIMIT]:
        match = json.loads(payload)
        summary, rank = match.get('summary') or {}, match.get('rank') or {}
        reviewed = progress['review'].get(ident)
        shown_rp = reviewed['value'] if reviewed else rank.get('delta')
        rp_source = reviewed['source'] if reviewed else 'settlement' if shown_rp is not None else None
        own = next((p for p in summary.get('players', []) if nickname(p.get('name') or '') == account), {})
        matches.append({
            'match': alias('match', ident), 'round': alias('round', match.get('round_id')),
            'time': _time(match.get('time')), 'created_at': _time(at),
            'mode': _option(match.get('mode'), MODES), 'current_season_split': (
                match.get('season'), match.get('split')) == (config['season'], config['split']),
            'record_source': _option(match.get('record_source'), ('last_snapshot', 'settlement', 'manual')),
            'status': record_status(match, config['self_name'])['label'],
            'deleted': bool(match.get('deleted_at')), 'merged_into': alias('match', match.get('merged_into')),
            'placement': _number(summary.get('placement'), 1, 100), 'player_count': len(summary.get('players', [])),
            'own': {key: _number(own.get(key), 0, 100_000) for key in ('kills', 'assists', 'knocks', 'damage')},
            'rank': _rank(rank),
            'displayed_rp': _number(shown_rp, -3000, 3000), 'displayed_rp_source': _option(rp_source, RANK_SOURCES),
            'evidence': [{'kind': kind, 'file': file_ref(row.get('path'), kind)} for row in match.get('evidence', [])
                         if (kind := _option(row.get('kind'), ('summary', 'rank','rank_overview','rank_transition','lobby','hud')))],
        })

    rounds = [
        {'round': alias('round', ident), 'game': alias('game', key), 'match': alias('match', mid),
         'started_at': _time(started), 'last_seen_at': _time(seen), 'discarded': bool(discarded),
         'phase': _option(phase, (*PHASES, 'alias', 'upload')), 'mode': _option(mode, MODES)}
        for ident, started, seen, phase, mid, discarded, mode, key in raw_rounds[:ROW_LIMIT]
    ]
    snapshots = []
    for at, payload, path, mid in raw_hud[:ROW_LIMIT]:
        hud = json.loads(payload)
        snapshots.append({'at': _time(at), 'file': file_ref(path, 'hud'),
                          'squad_file': file_ref(hud.get('squad_path'), 'hud'),
                          'match': alias('match', mid), 'round': alias('round', hud.get('round_id')),
                          'game': alias('game', hud.get('game_key')),
                          'live_rp': _number(hud.get('live_rp'), -3000, 3000), 'verified': bool(hud.get('verified'))})
    lobby = [
        {'at': _time(at), 'file': file_ref(path, 'lobby'), 'round': alias('round', rid), 'rank': _rank(json.loads(raw))}
        for at, raw, path, rid in raw_lobby[:ROW_LIMIT]
    ]
    files = [{**fields, **_file_metadata(path)} for path, fields in paths.items()]
    present={file['file'] for file in files if file['present']}
    hud_files={job['file'] for job in jobs if job['kind']=='hud'}
    for snapshot in snapshots:
        hud_files.update((snapshot['file'],snapshot['squad_file']))
    counts={
        'queued_jobs_in_report':len(jobs),
        'waiting_jobs_in_report':sum(job['status'] in ('pending','deferred') for job in jobs),
        'retained_hud_files_in_report':len(hud_files&present),
    }
    run_counts=runtime.get('capture_counts') or {}
    if match_id is None and isinstance(run_counts,dict):
        counts['current_run']={key:_number(run_counts.get(key),0,100_000_000) for key in (
            'probe_frames','hud_memory_candidates','processed_jobs','hud_tail_attempts',
        )}
    env = _environment(display)
    env.update(game_client_px=_size(runtime.get('game_size')),
               last_ocr_input_px=_size(runtime.get('ocr_size') or runtime.get('size')))
    return {
        'format': 'matchlog.capture-diagnostics', 'schema': 1,
        'version': version if type(version) is str and re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', version) else None,
        'exported_at': _time(now), 'window_hours': WINDOW_HOURS if match_id is None else None,
        'selection':'recent' if match_id is None else 'match',
        'environment': env, 'portable': bool(portable), 'recording': bool(running),
        'configuration': {key: bool(config[key]) for key in ('low_impact', 'auto_switch', 'debug_mode')},
        'profile_configured': bool(account), 'other_scope_job_count': other_scope_jobs,
        'displayed_total_rp': _number(progress.get('total_rp'), 0, 2_000_000),
        'current_run': {
            'started_at': _time(diagnostics.started_at),
            'events': [{'at': _time(at), 'event': code, 'phase': phase, 'job': job, 'round': rid}
                       for at, code, phase, job, rid in events],
        },
        'limits_reached': [label for label, rows, limit in (
            ('jobs', raw_jobs, JOB_LIMIT), ('matches', raw_matches, ROW_LIMIT), ('rounds', raw_rounds, ROW_LIMIT),
            ('hud_snapshots', raw_hud, ROW_LIMIT), ('lobby', raw_lobby, ROW_LIMIT)) if len(rows) > limit],
        'jobs': jobs, 'rounds': rounds, 'matches': matches, 'hud_snapshots': snapshots, 'lobby': lobby,
        'capture_counts': counts,
        'job_counts': dict(Counter(job['status'] or 'unknown' for job in jobs)),
        'file_counts': dict(Counter(file['kind'] for file in files if file['present'])),
        'files': files,
    }


def write_report(path, report):
    path = Path(path)
    if path.suffix.lower() != '.json':
        path = path.with_suffix('.json')
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.part', delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
