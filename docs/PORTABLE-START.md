# DiveTrail · 尾迹便携版

1. 把 ZIP 完整解压到有写入权限的文件夹，例如其他磁盘上的新文件夹。
2. 双击该文件夹里的 **DiveTrail.exe**。首次启动先看使用说明，再按提示打开 Apex 排位大厅识别昵称，也可手动填写。
3. 游戏结算后查看记录。需要补拍时，在 Apex 窗口按 F8。

数据保存在解压目录中的 **portable-data**。另一份解压目录会建立自己的数据。需要带走记录时，可用设置里的备份导出。

换版本前在旧版「设置」导出 `.matchlog` 备份。完全退出旧版，把新 ZIP 解压到新文件夹，启动后通过「设置」导入备份。原解压目录可保留一份，便于查阅旧设置和保留的截图。

试用环境为 Windows 10 或 11 x64。包内自带 Python 和 OCR 模型。系统需要 [Microsoft Edge WebView2 Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) 和 [Microsoft Visual C++ x64 Redistributable](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)。缺少组件时请使用微软安装程序。运行依赖版本记录于 runtime/requirements.lock，相应许可随包保留。
