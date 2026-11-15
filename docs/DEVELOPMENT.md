# 开发指南

在 Windows 仓库根目录运行以下命令。应用使用 Python 3.11 或更新版本与 Edge WebView2；JavaScript 测试使用 Node.js 24，运行桌面应用无需 Node.js。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
```

如果机器通过 `python` 而非 `py` 提供解释器，先检查 `python --version`，再用它创建虚拟环境。普通使用可直接运行 `launch.bat`，只安装 `requirements.txt` 中的运行依赖。维护工具的额外依赖在 `requirements-dev.txt`。

## 提交前检查

```powershell
.\.venv\Scripts\python.exe -m ruff check app.py matchlog tests tools
.\.venv\Scripts\python.exe -m ruff format --check tools/test.py tools/build_changelog.py tools/verify_desktop.py tools/verify_real_images.py
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -B tools/test.py
node --test tests/*.cjs
```

Ruff 从语法和执行错误起步，格式检查先覆盖测试、更新日志和验证工具。mypy 从已有类型标注的 RP 输入输出与发布说明边界起步。用 `tools/test.py test_rp_reconcile.py` 可单独运行一个 Python 测试文件。测试临时文件在 `output/tmp/`，Windows CI 使用 Python 3.11 和 3.13。CI 测试无需登录 Apex。

## 画面回放与实机

- `tools/inspect_samples.py <图片>` 输出 OCR 结果，适用于本地调查。分享输出前先移除账号、昵称及会话标识。
- `tools/verify_real_images.py --nickname <昵称> --rank <经验值截图> --summary <总结截图>` 用临时档案检查同局结算回放和重复证据。可另传 `--hud` 和 `--lobby` 样本。
- `tools/verify_desktop.py --empty` 用空白数据目录验证桌面窗口、界面通信和首次设置提示，生成 `output/native-window.json`。省略 `--empty` 时会复制已有数据库到独立的验证目录。报告和数据库副本留在本地。F8、全屏、不同 DPI 和实际游戏结算仍需真机验证。

## 构建 Windows 发行包

使用 Windows x64 的 Python 3.13 虚拟环境安装运行依赖，并先跑一次 OCR，使三份模型缓存到该环境。下载官方的 [Python 3.13.17 Windows x64 embeddable ZIP](https://www.python.org/downloads/release/python-31317/) 后执行以下命令。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tools/build_portable.py --public --python-zip .\output\python-3.13.17-embed-amd64.zip
```

构建器核对 Python 和 OCR 模型的 SHA-256，验证独立的运行时及离线 OCR，随后在 `output/release/` 生成 ZIP。依赖版本随包写入 `runtime/requirements.lock`。在干净 Windows 中解压并验证 [首次使用](PORTABLE-START.md)。

使用 `--installer --public` 构建安装 EXE。安装版用新版安装包更新；便携版的跨版本迁移详见[便携版说明](PORTABLE-START.md)。

## 维护和版本

用合成或脱敏样本为每个边界问题写最小回归。对局匹配应测试重连、不同场次混选和补录失败。数据库改动要测试已有库重复打开和备份恢复。

修改发布说明时先更新 `ui/changelog.json`，再运行 `tools/build_changelog.py` 同步 `CHANGELOG.md`。侧栏的版本号应与最新发布说明一致。发布前执行[发布检查清单](RELEASE-CHECKLIST.md)。
