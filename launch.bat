@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -c "import webview, rapidocr, onnxruntime, PIL, mss, numpy, cv2" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto error
)
start "" ".venv\Scripts\pythonw.exe" app.py
exit /b 0
:error
echo Setup failed. Please check Python and your network connection.
pause
exit /b 1
