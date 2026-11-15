"""Optional, manually confirmed community statistics via the CloudBase HTTP API."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from threading import RLock
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from matchlog.heroes import LABELS, canonical_hero
from matchlog.recognition import nickname
from matchlog.capture_diagnostics import format_events


ENVIRONMENT = 'matchlog-d0gaqb8vk26948890'
ENDPOINT = f'https://{ENVIRONMENT}.api.tcloudbasegateway.com'
RPC = '/v1/rdb/rest/rpc/'
MAX_BATCH = 100
DIAGNOSTIC_BATCH = 500
FEEDBACK_LIMIT = 1000
CHINA = timezone(timedelta(hours=8))


def anonymous_rows(matches, owner, seed, since, now):
    """Allowlist the public schema. Capture data and account names never leave this function."""
    owner = nickname(owner)
    hero_ids = {name: key for key, name in LABELS.items()} | {key: key for key in LABELS}
    rows = []
    for match in sorted(matches, key=lambda item: (item['created'], item['id'])):
        at = match['created']
        if (not owner or match.get('deleted_at') or match.get('merged_into')
                or not max(since, now - 366 * 86400) <= at <= now - 3600
                or match.get('record_source') in ('opening', 'last_snapshot')
                or match.get('record_origin') == 'upload' and not match.get('time_corrected')):
            continue
        summary = match.get('summary') or {}
        player = next((p for p in summary.get('players', []) if nickname(p.get('name', '')) == owner), {})
        mode, season, split = match.get('mode'), match.get('season'), str(match.get('split', ''))
        placement, damage = summary.get('placement'), player.get('damage')
        hero = hero_ids.get(canonical_hero(player.get('hero')), hero_ids.get(str(player.get('hero')).lower()))
        if (mode not in ('ranked', 'casual', 'event') or not re.fullmatch(r'S[0-9]{1,3}', season or '')
                or split not in ('1','2') or not hero or type(placement) is not int or not 1 <= placement <= 30
                or type(damage) is not int or not 0 <= damage <= 20000):
            continue
        ident = hashlib.sha256(f"{seed}:{match['id']}".encode()).hexdigest()
        rows.append({'key': ident, 'day': datetime.fromtimestamp(at, CHINA).date().isoformat(),
                     'season': season, 'split': split, 'mode': mode, 'hero': hero,
                     'placement': placement, 'damage': damage})
    return rows


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(path, body, headers):
    payload = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    req = Request(ENDPOINT + path, data=payload, headers={'Content-Type':'application/json', **headers})
    with build_opener(NoRedirect()).open(req, timeout=8) as response:
        return json.loads(response.read(128 * 1024))


class Community:
    def __init__(self, root, send=request, clock=time.time):
        self._path = Path(root) / 'community-state.json'
        self._send = send
        self._clock = clock
        self._lock = RLock()
        self._preview = None
        self._diagnostic_preview = None

    def _load(self):
        if not self._path.is_file():
            return {}
        return json.loads(self._path.read_text(encoding='utf-8'))

    def _save(self, state):
        path = self._path.with_suffix('.tmp')
        path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        path.replace(self._path)

    def status(self):
        with self._lock:
            state = self._load()
            return {'enabled': bool(state.get('enabled_at')), 'last_upload': state.get('last_upload')}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('请选择是否开启匿名上传')
        with self._lock:
            state = self._load()
            state.setdefault('device', secrets.token_hex(16))
            if enabled:
                state.setdefault('enabled_at', self._clock())
            else:
                state.pop('enabled_at', None)
                self._preview = None
            self._save(state)
            return self.status()

    def preview(self, matches, owner):
        with self._lock:
            state = self._load()
            if not state.get('enabled_at'):
                raise ValueError('请先开启匿名上传')
            rows = anonymous_rows(matches, owner, state['device'], state['enabled_at'], self._clock())
            sent = state.get('sent', {})
            rows = [row for row in rows if sent.get(row['key']) != self._digest(row)][:MAX_BATCH]
            token = secrets.token_urlsafe(18)
            self._preview = (token, owner, self._clock() + 300, rows)
            return {'token': token, 'count': len(rows)}

    @staticmethod
    def _digest(row):
        return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()

    def _token(self, state):
        if state.get('access_token') and state.get('expires_at', 0) > self._clock() + 30:
            return state['access_token']
        headers = {'x-device-id': state['device']}
        refresh = state.get('refresh_token')
        if refresh:
            auth = self._send('/auth/v1/token', {'grant_type':'refresh_token', 'refresh_token':refresh}, headers)
        else:
            auth = self._send('/auth/v1/signin/anonymously', {}, headers)
        if not isinstance(auth, dict) or not auth.get('access_token'):
            raise ValueError('腾讯云没有返回匿名访问令牌')
        state.update(access_token=auth['access_token'],
                     expires_at=self._clock() + int(auth.get('expires_in', 0)))
        if auth.get('refresh_token'):
            state['refresh_token'] = auth['refresh_token']
        self._save(state)
        return state['access_token']

    def _rpc(self, state, function, body):
        try:
            token = self._token(state)
            return self._send(RPC + function, body, {'Authorization': 'Bearer ' + token})
        except HTTPError as error:
            if error.code == 404:
                raise ValueError('腾讯云接口还未部署') from None
            raise ValueError(f'腾讯云拒绝请求（HTTP {error.code}）') from None
        except (URLError, TimeoutError, OSError):
            raise ValueError('暂时连不上腾讯云，稍后可以重试') from None

    def upload(self, token, owner, matches):
        with self._lock:
            state = self._load()
            draft = self._preview
            self._preview = None
            if (not state.get('enabled_at') or not draft or draft[0] != token or draft[1] != owner
                    or self._clock() > draft[2]):
                raise ValueError('上传预览已失效，请重新预览')
            rows = draft[3]
            current = {row['key']: row for row in anonymous_rows(
                matches, owner, state['device'], state['enabled_at'], self._clock())}
            if any(current.get(row['key']) != row for row in rows):
                raise ValueError('预览后有对局改动，请重新预览')
            if not rows:
                return {'count': 0}
            result = self._rpc(state, 'matchlog_submit_v1', {'matches': rows})
            if not isinstance(result, dict) or result.get('accepted') != len(rows):
                raise ValueError('服务端未确认全部对局，可重新预览后重试')
            sent = state.setdefault('sent', {})
            sent.update((row['key'], self._digest(row)) for row in rows)
            state['last_upload'] = self._clock()
            self._save(state)
            return {'count': len(rows)}

    def aggregate(self, season, split, mode):
        with self._lock:
            state = self._load()
            if not state.get('enabled_at'):
                raise ValueError('请先开启匿名上传')
            return self._rpc(state, 'matchlog_summary_v1', {
                'requested_season':season, 'requested_split':str(split), 'requested_mode':mode,
            })

    def preview_diagnostics(self, diagnostics, scope, version, feedback=''):
        if scope not in ('24h', 'all', 'none'):
            raise ValueError('请选择日志范围')
        if not isinstance(feedback, str):
            raise ValueError('反馈说明必须是文字')
        feedback = feedback.replace('\r\n', '\n').replace('\r', '\n').strip()
        if len(feedback) > FEEDBACK_LIMIT or re.search(r'[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]', feedback):
            raise ValueError(f'反馈说明限 {FEEDBACK_LIMIT} 字，请移除不可显示的字符')
        now = self._clock()
        rows = [] if scope == 'none' else diagnostics.retained_events(now - 86400 if scope == '24h' else 0, now)
        events = [(round(at * 1000), code, phase, job, rid) for at, code, phase, job, rid in rows]
        report = {'range': scope, 'version': version, 'events': events}
        if feedback:
            report['feedback'] = feedback
        receipt = self._digest(report)
        token = secrets.token_urlsafe(18) if rows or feedback else None
        with self._lock:
            self._diagnostic_preview = (token, now + 900, receipt, report) if token else None
        excerpt = rows[-400:]
        return {'token': token, 'count': len(rows), 'shown': len(excerpt), 'feedback': feedback,
                'first_at': rows[0][0] if rows else None, 'last_at': rows[-1][0] if rows else None,
                'text': format_events(excerpt) if excerpt else ''}

    def upload_diagnostics(self, token):
        with self._lock:
            draft = self._diagnostic_preview
            if not draft or draft[0] != token or self._clock() > draft[1]:
                raise ValueError('上传预览已失效，请重新预览')
            _, _, receipt, report = draft
            state = self._load()
            if 'device' not in state:
                state['device'] = secrets.token_hex(16)
                self._save(state)
            events = report['events']
            batch_count = max(1, (len(events) + DIAGNOSTIC_BATCH - 1) // DIAGNOSTIC_BATCH)
            for index in range(batch_count):
                offset = index * DIAGNOSTIC_BATCH
                batch = events[offset:offset + DIAGNOSTIC_BATCH]
                payload = {
                    'receipt': receipt, 'range': report['range'], 'version': report['version'],
                    'index': index, 'total': batch_count, 'events': batch,
                }
                if index == 0 and 'feedback' in report:
                    payload['feedback'] = report['feedback']
                function = 'matchlog_submit_diagnostics_v2' if 'feedback' in payload else 'matchlog_submit_diagnostics_v1'
                result = self._rpc(state, function, {'batch': payload})
                if not isinstance(result, dict) or result.get('accepted') != len(batch):
                    raise ValueError('腾讯云未确认完整反馈，可以重试提交')
            self._diagnostic_preview = None
            return {'count': len(events), 'receipt': receipt[:16]}
