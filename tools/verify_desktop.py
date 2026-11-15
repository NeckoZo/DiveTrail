"""Smoke-test the native window in a separate data directory."""

import argparse
from contextlib import closing
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app


def wait_for(predicate, attempts=50):
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    database = parser.add_mutually_exclusive_group()
    database.add_argument(
        "--empty",
        action="store_true",
        help="Use a new database without copying existing records.",
    )
    database.add_argument(
        "--overview",
        action="store_true",
        help="Verify the overview and logs with synthetic matches.",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Exercise the native notification area menu.",
    )
    args = parser.parse_args()
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="native-probe-", dir=output))
    report = output / "native-window.json"
    expected_version = json.loads(
        (ROOT / "ui/changelog.json").read_text(encoding="utf-8")
    )["version"]
    source_path = ROOT / "data/matches.sqlite3"
    if not args.empty and not args.overview and source_path.exists():
        with (
            closing(
                sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(data / "matches.sqlite3")) as target,
        ):
            source.backup(target)

    result: dict[str, object] = {}
    shutdown_complete = threading.Event()

    class ProbeApi(app.Api):
        def __init__(self):
            super().__init__(data, desktop=True)
            self._title = "MatchLog Verification"
            self._running = False
            self._probe_state_ready = threading.Event()
            self._store.configure(
                {
                    "auto_cleanup": False,
                    "debug_mode": False,
                    "start_on_login": False,
                    "auto_switch": False,
                }
            )
            if args.overview:
                self._seed_overview()

        def _seed_overview(self):
            from matchlog.community import Community

            self._network_attempts = []
            self._community_clock = time.time() - 3 * 3600

            def forbid_network(path, _body, _headers):
                self._network_attempts.append(path)
                raise AssertionError(
                    "Network access is disabled in the native smoke test"
                )

            self._community = Community(
                data, send=forbid_network, clock=lambda: self._community_clock
            )
            self._store.configure(
                {
                    "self_name": "Overview Probe",
                    "friends": ["Probe A", "Probe B"],
                    "season": "S31",
                    "split": "1",
                    "auto_season": False,
                }
            )
            for index, place in enumerate((1, 4, 9)):
                at = datetime.fromtimestamp(
                    time.time() - 4500 - 300 * index, timezone.utc
                )
                self._store.create_manual(
                    {
                        "time": at.isoformat(),
                        "placement": place,
                        "delta": 85 - 30 * index,
                        "players": [
                            {
                                "name": "Overview Probe",
                                "hero": "Vantage",
                                "kills": 3,
                                "assists": 2,
                                "knocks": 4,
                                "damage": 915,
                            },
                            {
                                "name": "Probe A",
                                "hero": "Lifeline",
                                "kills": 2,
                                "assists": 1,
                                "knocks": 2,
                                "damage": 555,
                            },
                            {
                                "name": "Probe B",
                                "hero": "Pathfinder",
                                "kills": 1,
                                "assists": 1,
                                "knocks": 1,
                                "damage": 389,
                            },
                        ],
                    }
                )

        def state(self):
            payload = super().state()
            self._probe_state_ready.set()
            return payload

        def poll_state(self, revision=None, include_recovery=False):
            packet = super().poll_state(revision, include_recovery)
            if "live" in packet:
                self._probe_state_ready.set()
            return packet

        def _start(self):
            super()._start()
            threading.Thread(target=self.probe, daemon=True).start()

        def _shutdown(self):
            super()._shutdown()
            shutdown_complete.set()

        def probe_tray(self):
            from System import Action

            form = self._window.native
            tray = self._tray
            menu = tray._notification.ContextMenuStrip

            def click(index):
                form.Invoke(Action(lambda: menu.Items[index].PerformClick()))

            # The default probe opens minimized. Restore through the native tray menu.
            click(0)
            click(1)
            started = wait_for(lambda: self._running and not tray._changing.locked())
            user32 = C.WinDLL("user32")
            user32.IsWindowVisible.argtypes = [W.HWND]
            user32.IsWindowVisible.restype = W.BOOL
            hwnd = form.Handle.ToInt64()
            self._window.evaluate_js(
                "document.querySelector('[data-window=close]').click()"
            )
            result["tray_keeps_recording"] = bool(
                started
                and wait_for(lambda: not user32.IsWindowVisible(hwnd))
                and self._running
            )
            click(1)
            result["tray_pauses"] = wait_for(
                lambda: not self._running and not tray._changing.locked()
            )
            click(0)
            result["tray_reopens"] = wait_for(lambda: user32.IsWindowVisible(hwnd))
            result["ok"] = bool(
                result["ok"]
                and all(
                    result[key]
                    for key in ("tray_keeps_recording", "tray_pauses", "tray_reopens")
                )
            )
            click(2)

        def probe_overview(self):
            evaluate = self._window.evaluate_js
            evaluate("document.querySelector('#capture-diagnostics-dialog').close()")
            if not wait_for(
                lambda: evaluate(
                    "document.querySelectorAll('#recent-table tbody tr').length === 3"
                )
            ):
                raise RuntimeError(
                    "The overview did not load all three synthetic records"
                )
            result["overview_viewport"] = evaluate("""(() => {
                const rows = Array.from(document.querySelectorAll('#recent-table tbody tr'), row => {
                    const {top, bottom, left, right} = row.getBoundingClientRect();
                    return {top, bottom, left, right};
                });
                const blocks = Object.fromEntries(['.topbar','.content','.page-heading','#profile-notice',
                    '#profile-change-notice','.overview-tools','.hero-grid','.rank-card','.trend-panel',
                    '#chart','#metrics','.section-title','#recent-table'].map(selector => {
                    const el = document.querySelector(selector);
                    const {y,height} = el.getBoundingClientRect();
                    return [selector, [Math.round(y), Math.round(height), getComputedStyle(el).display]];
                }));
                return {width: innerWidth, height: innerHeight, dpi: devicePixelRatio, rows, blocks,
                        visibleRows: rows.filter(r => r.top >= 0 && r.bottom <= innerHeight &&
                                                     r.left >= 0 && r.right <= innerWidth).length};
            })()""")
            result["usage_guide"] = evaluate("""(() => {
                document.querySelector('#usage-btn').click();
                const dialog = document.querySelector('#usage-dialog');
                const actions = dialog.querySelectorAll('.dialog-footer button');
                const text = dialog.textContent;
                const ready = dialog.open && actions.length === 1 &&
                    actions[0].textContent === '返回' && text.includes('全自动识别') &&
                    text.includes('F8') && text.includes('提示音');
                dialog.close();
                return ready;
            })()""")
            evaluate("document.querySelector('#settings-btn').click()")
            result["recording_in_settings"] = bool(
                evaluate("""
                document.querySelector('#settings-dialog').open &&
                document.querySelector('#settings-dialog #record-control').textContent === '继续记录'
            """)
            )
            if result["recording_in_settings"]:
                evaluate("document.querySelector('#record-control').click()")
                resumed = wait_for(
                    lambda: evaluate(
                        "document.querySelector('#record-control').textContent === '暂停记录'"
                    )
                )
                if resumed:
                    evaluate("document.querySelector('#record-control').click()")
                result["recording_in_settings"] = resumed and wait_for(
                    lambda: evaluate(
                        "document.querySelector('#record-control').textContent === '继续记录'"
                    )
                )
            evaluate("document.querySelector('#settings-dialog').close()")
            result["share"] = evaluate("""(() => {
                document.querySelector('#share-overview-btn').click();
                const canvas = document.querySelector('#share-card');
                const pixel = canvas.getContext('2d').getImageData(4, 4, 1, 1).data;
                const dialog = document.querySelector('#share-overview-dialog');
                const ready = dialog.open && document.querySelector('#share-hide-names').checked && pixel[3] > 0;
                dialog.close();
                return ready;
            })()""")
            self._community_clock = time.time() + 1
            self._diagnostics.add("reading", phase="rank")
            evaluate("document.querySelector('#capture-diagnostics-btn').click()")
            result["anonymous_can_submit_directly"] = wait_for(
                lambda: evaluate("""
                document.querySelector('#capture-diagnostics-dialog').open &&
                document.querySelector('#diagnostics-range input:checked').value === '24h' &&
                !document.querySelector('#diagnostics-submit').disabled &&
                document.querySelector('#diagnostics-upload-status').textContent.includes('匿名')
            """)
            )
            evaluate("document.querySelector('#diagnostics-preview').click()")
            result["anonymous_preview"] = wait_for(
                lambda: evaluate("""
                !document.querySelector('#diagnostics-submit').disabled &&
                document.querySelector('#diagnostics-upload-status').textContent.includes('匿名事件')
            """)
            )
            result["anonymous_network_attempts"] = len(self._network_attempts)
            result["ok"] = bool(
                result["ok"]
                and result["overview_viewport"]["visibleRows"] == 3
                and result["usage_guide"]
                and result["recording_in_settings"]
                and result["share"]
                and result["anonymous_can_submit_directly"]
                and result["anonymous_preview"]
                and not self._network_attempts
            )

        def probe(self):
            exiting_via_tray = False
            try:
                for _ in range(60):
                    time.sleep(0.25)
                    if not self._probe_state_ready.is_set():
                        continue
                    raw = self._window.evaluate_js(
                        """JSON.stringify({
                            bridge: !!window.pywebview?.api,
                            version: document.querySelector('#changelog-btn')?.textContent,
                            profile: document.querySelector('#profile-name')?.textContent,
                            setup: document.querySelector('#profile-notice')?.hidden === false
                                && document.querySelector('#profile-setup')?.hidden === false,
                            message: document.querySelector('#profile-notice-message')?.textContent,
                            theme: document.documentElement.dataset.theme
                        })"""
                    )
                    if not raw:
                        continue
                    page = json.loads(raw) if isinstance(raw, str) else raw
                    if not (
                        page.get("bridge") and page.get("version") == expected_version
                    ):
                        continue
                    if args.empty and not (
                        page.get("setup")
                        and "昵称" in (page.get("message") or "")
                        and page.get("profile") == "待设置"
                    ):
                        continue
                    result.update(page)
                    break
                if not result:
                    raise RuntimeError(
                        "Timed out waiting for the WebView bridge and expected setup state"
                    )
                if args.empty:
                    result["first_run_guide"] = bool(
                        self._window.evaluate_js("""
                        document.querySelector('#usage-dialog').open &&
                        !document.querySelector('#profile-setup-dialog').open
                    """)
                    )
                    if result["first_run_guide"]:
                        self._window.evaluate_js(
                            "document.querySelector('#usage-return').click()"
                        )
                    result["setup_dialog"] = bool(
                        self._window.evaluate_js(
                            "document.querySelector('#profile-setup-dialog').open"
                        )
                    )
                    self._window.evaluate_js(
                        "document.querySelector('#profile-setup-dialog').close()"
                    )
                else:
                    result["whats_new"] = bool(
                        self._window.evaluate_js(
                            "document.querySelector('#changelog-dialog').open"
                        )
                    )
                    self._window.evaluate_js(
                        "document.querySelector('#changelog-dialog').close()"
                    )
                self._window.evaluate_js(
                    "document.querySelector('#capture-diagnostics-btn').click()"
                )
                for _ in range(20):
                    time.sleep(0.15)
                    if self._window.evaluate_js(
                        """document.querySelector('#capture-diagnostics-dialog').open &&
                            document.querySelector('#capture-diagnostics-text').readOnly &&
                            document.querySelector('#capture-diagnostics-text').value.includes('采集日志')"""
                    ):
                        result["diagnostics"] = True
                        break
                user32 = C.WinDLL("user32")
                user32.FindWindowW.argtypes = [W.LPCWSTR, W.LPCWSTR]
                user32.FindWindowW.restype = W.HWND
                result["native_window"] = bool(user32.FindWindowW(None, self._title))
                if args.empty:
                    settings = self._store.settings()
                    result["clean_profile"] = (
                        not settings["self_name"]
                        and not settings["friends"]
                        and not self._store.all()
                    )
                result["ok"] = bool(
                    result["native_window"]
                    and result.get("diagnostics")
                    and (
                        not args.empty
                        or (
                            result.get("clean_profile")
                            and result.get("first_run_guide")
                            and result.get("setup_dialog")
                        )
                    )
                )
                if args.overview:
                    self.probe_overview()
                if args.tray:
                    self.probe_tray()
                    exiting_via_tray = True
            except Exception as error:
                result.update(ok=False, error=str(error))
            finally:
                if not exiting_via_tray:
                    self._window.destroy()

    app.Api = ProbeApi
    sys.argv = ["app.py"] if args.overview else ["app.py", "--startup"]
    try:
        app.main(instance_name=r"Local\MatchLogVerification")
    except Exception as error:
        result.update(ok=False, error=str(error))
    if not result:
        result.update(ok=False, error="Verification window did not open")
    if args.tray and result.get("ok"):
        result["tray_shutdown"] = shutdown_complete.wait(timeout=10)
        result["ok"] = result["tray_shutdown"]
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))
    return int(not result.get("ok"))


if __name__ == "__main__":
    raise SystemExit(main())
