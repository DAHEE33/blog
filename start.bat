@echo off
cd /d "%~dp0"
py -3 --version >nul 2>&1
if errorlevel 1 (
  python app.py
) else (
  py -3 app.py
)
pause
