"""Bounded local capture trail with anonymous counters."""

from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from threading import Lock
import time


CHINA = timezone(timedelta(hours=8))
PHASES = {
    'hud': '局内', 'lobby': '大厅', 'lobby_candidate': '大厅候选', 'settlement': '结算',
    'summary': '战绩总结', 'rank': '排位分数', 'rank_overview': '排位目标',
    'death_recap': '死亡回放', 'postmatch': '结算切页', 'spectator': '观战', 'profile': '个人资料',
    'training': '靶场', 'intro': '排位倒计时', 'legend_select': '英雄选择', 'loading': '等待进入对局',
    'manual': '手动补拍', 'probe': '探测截图', 'unknown': '未识别', 'other': '未匹配界面模板',
}
EVENTS = {
    'no_profile': '没有截图 · 请先设置游戏昵称',
    'no_game': '没有截图 · 等待 Apex 前台',
    'manual_requested': '已请求手动补拍',
    'paused': '自动采集已暂停',
    'resumed': '自动采集已恢复',
    'manual_expired': '没有截图 · 补拍请求已过期',
    'focus_lost': '没有保存截图 · 游戏窗口已切换',
    'black_frame': '没有保存截图 · 画面为黑屏',
    'capture_error': '没有保存截图 · 截图或写入失败',
    'hotkey_unavailable': 'F8 暂不可用，可用补拍按钮',
    'hotkey_available': 'F8 可用',
    'seen': '画面探测',
    'queued': '截图已保存 · 等待识别',
    'deferred': '截图已保存 · 游戏优先模式延后识别',
    'queue_skipped': '已读取画面 · 本次未新增采集任务',
    'duplicate': '相同画面已处理 · 跳过重复截图',
    'reading': '开始识别已保存的截图',
    'recognized': '已识别画面类型',
    'unknown': '有截图 · 没有识别出画面类型',
    'wrong_scope': '已留图 · 采集时的档案或赛段与当前不同',
    'game_key_missing': '已识别结算 · 未读到明确的场次标识',
    'round_relinked': '已按场次标识重新关联截图',
    'unstable': '已识别 · 等待另一帧确认数值',
    'tally_pending': '已识别分数页 · 排位明细仍在变化',
    'overview_only': '已识别排位目标页 · 等待实际结算分数',
    'missing_rp': '已识别分数页 · 未读到加减分',
    'identity_unconfirmed': '已识别画面 · 无法确认是本人',
    'record_blocked': '已识别结算 · 场次或记录状态不允许写入',
    'saved': '已入库 · 本页已确认',
    'partial': '已入库 · 部分数据待补',
    'auxiliary': '已保存快照或资料',
    'lobby_only': '已识别大厅 · 未读到段位分数',
    'ignored': '已处理 · 该画面无需记入战绩',
    'unchanged': '已有同等或更新的证据',
    'review': '截图已保留 · 等待核对',
    'recognition_error': '有截图 · 识别或入库失败，原图保留',
    'fallback': '已用最后一次局内快照保底',
    'profile_waiting': '建档 · 等待 Apex 前台',
    'profile_lobby': '建档 · 等待确认大厅',
    'profile_reading': '建档 · 正在读取昵称和段位',
    'profile_mode': '建档 · 请切换到排位赛',
    'profile_name_missing': '建档 · 未读到可确认的昵称',
    'profile_confirming': '建档 · 正在核对第二张画面',
    'profile_ready': '建档 · 已找到昵称，等待确认',
    'profile_timeout': '建档 · 识别超时，可手动填写',
    'profile_capture_error': '建档 · 未取得有效游戏画面',
    'retry_saved': '已请求重识别原图',
    'open_round': '排位倒计时已记录',
}
LOG_NAME = re.compile(r'capture-(\d{6}|\d{8})\.jsonl(?:\.old)?')


def _read_rows(path):
    try:
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = tuple(json.loads(line))
                    if (len(row) != 5 or row[1] not in EVENTS or type(row[0]) not in (int, float)
                            or not 100_000_000 <= row[0] < 10_000_000_000):
                        continue
                    if row[2] is not None and row[2] not in PHASES:
                        continue
                    if any(x is not None and (type(x) is not int or not 1 <= x <= 9_999_999_999)
                           for x in row[3:]):
                        continue
                except (TypeError, ValueError):
                    continue
                yield row
    except (OSError, ValueError):
        return


def format_events(events):
    lines = ['DiveTrail · 尾迹 采集日志 · 北京时间']
    for at, code, phase, job, round_id in events:
        details = [PHASES[phase]] if phase else []
        if job is not None:
            details.append(f'截图 #{job}')
        if round_id is not None:
            details.append(f'场次 #{round_id}')
        suffix = ' [' + ' / '.join(details) + ']' if details else ''
        stamp = datetime.fromtimestamp(at, CHINA).strftime('%m-%d %H:%M:%S')
        lines.append(f'{stamp}  {EVENTS[code]}{suffix}')
    if not events:
        lines.append('还没有采集事件。')
    return '\n'.join(lines)


class CaptureDiagnostics:
    def __init__(self, limit=180, folder=None):
        self.started_at = time.time()
        self._events = deque(maxlen=limit)
        self._lock = Lock()
        self._aliases = OrderedDict()
        self._next_alias = 0
        self._limit = limit
        self._last_throttled = {}
        self._folder = Path(folder) if folder is not None else None
        self._day = None
        if self._folder is not None:
            self._load()

    def _load(self):
        cutoff = datetime.now(CHINA).date() - timedelta(days=6)
        def log_order(path):
            found=LOG_NAME.fullmatch(path.name)
            stamp=found[1] if found else ''
            return (('20'+stamp) if len(stamp)==6 else stamp,not path.name.endswith('.old'))
        for path in sorted(self._folder.glob('capture-*.jsonl*'),key=log_order):
            found = LOG_NAME.fullmatch(path.name)
            if not found or not path.is_file():
                continue
            try:
                stamp=found[1]
                day = datetime.strptime(stamp,'%y%m%d' if len(stamp)==6 else '%Y%m%d').date()
                if day < cutoff:
                    continue
                for row in _read_rows(path):
                    self._events.append(row)
                    self._next_alias = max(self._next_alias, row[3] or 0, row[4] or 0)
            except (OSError, ValueError):
                continue
        self._events = deque(sorted(self._events, key=lambda row:row[0]), maxlen=self._limit)

    def _persist(self, row):
        day = datetime.fromtimestamp(row[0], CHINA).date()
        try:
            if self._day != day:
                self._folder.mkdir(parents=True, exist_ok=True)
                cutoff = (day - timedelta(days=6)).strftime('%Y%m%d')
                for path in self._folder.glob('capture-*.jsonl*'):
                    found = LOG_NAME.fullmatch(path.name)
                    stamp=found[1] if found else ''
                    if stamp and (('20'+stamp) if len(stamp)==6 else stamp) < cutoff:
                        path.unlink(missing_ok=True)
                self._day = day
            path = self._folder / ('capture-' + day.strftime('%y%m%d') + '.jsonl')
            if path.exists() and path.stat().st_size >= 700_000:
                path.replace(path.with_suffix('.jsonl.old'))
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row, separators=(',', ':')) + '\n')
        except OSError:
            # A full disk must never stop capture.
            return

    def _alias(self, category, value):
        if value is None:
            return None
        key = (category, value)
        number = self._aliases.get(key)
        if number is None:
            self._next_alias += 1
            number = self._next_alias
            self._aliases[key] = number
        self._aliases.move_to_end(key)
        if len(self._aliases) > self._limit * 2:
            self._aliases.popitem(last=False)
        return number

    def add(self, code, *, phase=None, job=None, round_id=None, every=None):
        if code not in EVENTS:
            return
        if phase not in PHASES:
            phase = None
        with self._lock:
            if every is not None:
                now = time.monotonic()
                key = (code, phase)
                previous = self._last_throttled.get(key)
                if previous is not None and now - previous < every:
                    return
                self._last_throttled[key] = now
            row = (
                time.time(), code, phase,
                self._alias('job', job), self._alias('round', round_id),
            )
            self._events.append(row)
            if self._folder is not None:
                self._persist(row)

    def snapshot(self, since):
        """Return the structured trail and its internal alias map for a local export."""
        with self._lock:
            return tuple(row for row in self._events if row[0] >= since), dict(self._aliases)

    def report(self):
        with self._lock:
            return format_events(tuple(self._events))

    def retained_events(self, since, now):
        """Include the rotated files so an upload is not limited to the on-screen trail."""
        with self._lock:
            rows = set(self._events)
            if self._folder is not None:
                for path in self._folder.glob('capture-*.jsonl*'):
                    if LOG_NAME.fullmatch(path.name):
                        rows.update(_read_rows(path))
        return sorted((row for row in rows if since <= row[0] <= now), key=lambda row: row[0])
