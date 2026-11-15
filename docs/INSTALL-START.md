# DiveTrail · 尾迹安装版

1. 从托盘完全退出尾迹或旧版 Apex MatchLog，再双击 `DiveTrail-*-setup-win-x64.exe`。
2. 从 Windows 开始菜单打开 **DiveTrail**。程序安装在当前账户的 `%LOCALAPPDATA%\Programs\DiveTrail`，无需管理员权限。
3. 以后更新时，先完全退出程序，再运行新版安装包。程序目录会更新，对局数据留在 Windows「文档」。
4. 对局、设置和截图保存在 Windows「文档」下的 `DiveTrail` 文件夹。若 Windows 已重定向「文档」，应用使用重定向后的目录。

从旧安装版升级前，建议先在「设置」导出 `.matchlog` 备份。若「文档」只有旧版 `Apex MatchLog` 数据目录，首次启动会尝试迁移到 `DiveTrail`。两个目录都存在时使用 `DiveTrail`，可从旧版备份导入历史对局。遇到文件系统阻止迁移时会沿用原目录。旧程序及快捷方式仍保留，升级后请使用新的 DiveTrail 入口，旧任务栏固定项可重新固定。

从源码版或便携版迁移时，在旧应用的「设置」导出 `.matchlog` 备份，再在安装版导入。便携版在解压目录下使用 `portable-data`。未确认的头像、已清理的原图和 WebView 缓存不随备份导入。

卸载时关闭应用，删除安装目录及开始菜单中的快捷方式。若已启用开机自启，请先在应用设置中关闭。要保留记录，请保留「文档」中的数据目录。

支持 Windows 10 或 11 x64。应用自带 Python 和 OCR 模型。系统需要 Microsoft Edge WebView2 Evergreen Runtime 和 Microsoft Visual C++ x64 Redistributable。[WebView2 下载](https://developer.microsoft.com/microsoft-edge/webview2/) · [Visual C++ 下载](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)

运行依赖及许可随安装包提供。
