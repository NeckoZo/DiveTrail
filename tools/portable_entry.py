"""Display an actionable error if the portable window cannot initialize."""

import ctypes
from pathlib import Path
import runpy
import sys
import traceback


def launch():
    root = Path(__file__).resolve().parent
    if (root / '.matchlog-update-journal').exists():
        ctypes.windll.user32.MessageBoxW(None, '升级尚未完成。请再次运行升级程序恢复。', 'DiveTrail · 尾迹', 0x10)
        return
    try:
        runpy.run_path(str(root / 'app.py'), run_name='__main__')
    except Exception:
        try:
            data = root / 'portable-data'
            if '--installed' in sys.argv:
                from matchlog.windows_install import installed_data_path
                data = installed_data_path(migrate=False)
            log = data / 'startup-error.log'
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding='utf-8')
            message = f'尾迹启动失败。详细信息位于 {log}。'
        except OSError:
            message = ('尾迹无法写入「文档」中的数据文件夹。' if '--installed' in sys.argv else
                       '尾迹无法写入便携数据。请将 ZIP 解压到你有写入权限的文件夹后重试。')
        ctypes.windll.user32.MessageBoxW(None, message, 'DiveTrail · 尾迹', 0x10)


if __name__ == '__main__':
    launch()
