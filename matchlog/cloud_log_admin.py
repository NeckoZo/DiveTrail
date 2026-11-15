"""Export owner-accessible CloudBase diagnostic logs through the local CLI."""

import argparse
from getpass import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import Lock
import time

from matchlog.community import ENVIRONMENT


OWNER_CONFIG = 'cloud-log-owner.json'
PAGE_SIZE = 8
PIN_PATTERN = re.compile(r'[0-9]{6}')
PIN_ITERATIONS = 600_000


def device_fingerprint():
    if sys.platform != 'win32':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                           0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid, _ = winreg.QueryValueEx(key, 'MachineGuid')
        user = os.getlogin().casefold()
    except OSError:
        return None
    return hashlib.sha256(f'MatchLog diagnostic admin v1:{guid}:{user}'.encode()).hexdigest()


def _pin_digest(pin, salt):
    return hashlib.pbkdf2_hmac('sha256', pin.encode('ascii'), bytes.fromhex(salt), PIN_ITERATIONS).hex()


def enroll(data, pin=None):
    fingerprint = device_fingerprint()
    if not fingerprint:
        raise ValueError('未能读取 Windows 设备身份')
    settings = {'device': fingerprint}
    if pin is not None:
        if not PIN_PATTERN.fullmatch(pin):
            raise ValueError('口令需为六位数字')
        salt = secrets.token_hex(32)
        settings.update(pin_salt=salt, pin_digest=_pin_digest(pin, salt))
    path = Path(data) / OWNER_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(settings), encoding='utf-8')
    temp.replace(path)


def _cli(source):
    node = shutil.which('node')
    wrapper = shutil.which('tcb')
    candidates = [Path(source) / 'output/cloud-admin/node_modules/@cloudbase/cli/bin/tcb']
    if wrapper:
        candidates.append(Path(wrapper).parent / 'node_modules/@cloudbase/cli/bin/tcb')
    if node:
        for candidate in candidates:
            if candidate.is_file():
                return [node, str(candidate)]
    if wrapper and Path(wrapper).suffix.lower() == '.exe':
        return [wrapper]
    return None


def query_cloud_rows(command, sql):
    try:
        process = subprocess.run(
            [*command, 'db', 'execute', '-e', ENVIRONMENT, '--sql', sql, '--json'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        result = json.loads(process.stdout.decode('utf-8'))
        raw = result['data']['Rows']
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError('Malformed CloudBase rows')
        return [json.loads(row) if isinstance(row, str) else row for row in raw]
    except (OSError, subprocess.SubprocessError, UnicodeError, KeyError, TypeError, ValueError):
        raise ValueError('腾讯云查询失败，请检查 CLI 登录和数据库权限。') from None


def _literal(value):
    return f"convert_from(decode('{value.encode('utf-8').hex()}', 'hex'), 'UTF8')"


def write_export(target, query):
    """Stream a bounded snapshot and publish it only after all pages arrive."""
    snapshot_rows = query('SELECT extract(epoch FROM clock_timestamp())::text')
    stamp = str(snapshot_rows[0][0]) if len(snapshot_rows) == 1 and snapshot_rows[0] else ''
    if not re.fullmatch(r'[0-9]{1,11}(?:\.[0-9]{1,9})?', stamp):
        raise ValueError('无法确认云端导出时间。')
    target = Path(target)
    batch_count = event_count = report_count = 0
    cursor = None
    temp = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                dir=target.parent, suffix='.part', delete=False) as stream:
            temp = Path(stream.name)

            def write(value):
                stream.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')

            write({'format': 'matchlog.cloud-diagnostics', 'schema': 1, 'snapshot_at_epoch': float(stamp)})
            while True:
                bounds = f'received_at <= to_timestamp({stamp})'
                if cursor is not None:
                    owner, receipt, index = cursor
                    bounds += f' AND (owner, receipt, batch_index) > ({_literal(owner)}, {_literal(receipt)}, {index})'
                rows = query(
                    'SELECT owner, receipt, batch_index, batch_total, requested_range, client_version, '
                    "received_at::text, events::text, to_jsonb(batch)->>'feedback' "
                    'FROM matchlog_private.capture_log_batches AS batch '
                    f'WHERE {bounds} ORDER BY owner, receipt, batch_index LIMIT {PAGE_SIZE}'
                )
                for owner, receipt, index, total, scope, version, received_at, encoded, feedback in rows:
                    index = int(index)
                    next_cursor = (owner, receipt, index)
                    if cursor == next_cursor:
                        raise ValueError('云端日志分页顺序异常。')
                    if cursor is None or cursor[:2] != next_cursor[:2]:
                        report_count += 1
                    cursor = next_cursor
                    events = json.loads(encoded)
                    item = {
                        'report': report_count, 'receipt': receipt[:16], 'range': scope,
                        'version': version, 'received_at': received_at, 'batch_index': index,
                        'batch_total': int(total), 'events': events,
                    }
                    if feedback:
                        item['feedback'] = feedback
                    write(item)
                    batch_count += 1
                    event_count += len(events)
                if len(rows) < PAGE_SIZE:
                    break
            stats = {'reports': report_count, 'batches': batch_count, 'events': event_count}
            write({'complete_export': True, **stats})
        temp.replace(target)
        return {'ok': True, **stats}
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


class CloudLogAdmin:
    def __init__(self, data, source):
        self._settings = Path(data) / OWNER_CONFIG
        self._source = source
        self._lock = Lock()
        self._failed = 0
        self._blocked_until = 0.0

    def _access(self):
        try:
            settings = json.loads(self._settings.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False, None
        if not isinstance(settings, dict):
            return False, None
        owner = settings.get('device')
        fingerprint = device_fingerprint()
        is_owner = bool(fingerprint and isinstance(owner, str) and hmac.compare_digest(fingerprint, owner))
        salt, digest = settings.get('pin_salt'), settings.get('pin_digest')
        credentials = (salt, digest) if all(
            isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in (salt, digest)
        ) else None
        return is_owner, credentials

    def status(self):
        is_owner, credentials = self._access()
        return {'available': bool((is_owner or credentials) and _cli(self._source)), 'requires_pin': not is_owner}

    def authorize(self, pin):
        is_owner, credentials = self._access()
        if not is_owner:
            if credentials is None:
                raise ValueError('只有已登记的管理端可以下载云端日志。')
            with self._lock:
                if time.monotonic() < self._blocked_until:
                    raise ValueError('请稍后再试。')
                salt, digest = credentials
                if not (isinstance(pin, str) and PIN_PATTERN.fullmatch(pin)
                        and hmac.compare_digest(digest, _pin_digest(pin, salt))):
                    self._failed += 1
                    if self._failed >= 5:
                        self._blocked_until = time.monotonic() + 60
                        self._failed = 0
                    raise ValueError('管理口令不正确。')
                self._failed = 0
        command = _cli(self._source)
        if not command:
            raise ValueError('请先安装并登录 CloudBase CLI。')
        return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['enroll'])
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--device-only', action='store_true', help='Register this Windows device only')
    args = parser.parse_args()
    if not args.device_only and not sys.stdin.isatty():
        parser.error('Enter the PIN in an interactive terminal without echo.')
    try:
        pin = None if args.device_only else getpass('管理口令（六位数字）: ')
        if pin is not None and pin != getpass('再次输入管理口令: '):
            raise ValueError('两次口令不一致')
        enroll(args.data, pin)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print('已登记本机管理权限。')


if __name__ == '__main__':
    main()
