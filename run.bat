@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo .venv not found.  Please run install.bat first.
    pause
    exit /b 1
)
rem pythonw = 콘솔 창 없이 실행 (빈 콘솔 안 뜸). start 로 띄워 이 창도 바로 닫음.
start "" ".venv\Scripts\pythonw.exe" myah.py
