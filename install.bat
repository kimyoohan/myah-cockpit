@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo    myah - installer
echo ============================================
echo.

REM --- find a working Python (prefer the py launcher) ---
set "PYEXE="
py -3 --version >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
    python --version >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] Python 3.10+ not found.
    echo   Install from https://www.python.org/downloads/  ^(check "Add python.exe to PATH"^)
    echo   If typing 'python' opens the Microsoft Store, turn OFF the alias:
    echo     Settings ^> Apps ^> App execution aliases ^> python.exe / python3.exe
    echo.
    pause
    exit /b 1
)

REM --- create venv ---
if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Creating virtual environment with: %PYEXE%
    %PYEXE% -m venv .venv
)

REM --- verify venv really got created (clear error instead of a cryptic path error) ---
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [ERROR] Could not create .venv  ^( .venv\Scripts\python.exe is missing ^).
    echo   Most likely 'python' is the Microsoft Store placeholder, so venv was not made.
    echo   Fix one of these, then run install.bat again:
    echo     1^) Install real Python from https://www.python.org/downloads/  ^(check Add to PATH^)
    echo     2^) Settings ^> Apps ^> App execution aliases ^> turn OFF python.exe / python3.exe
    echo.
    pause
    exit /b 1
)

REM --- install dependencies ---
echo [2/2] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency install failed. See messages above.
    pause
    exit /b 1
)

REM --- check Chrome (warning only) ---
if not exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" if not exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
    echo.
    echo [WARN] Google Chrome not found. myah needs Chrome for the left preview.
    echo        Get it at https://www.google.com/chrome/
)

REM --- optional desktop shortcut ---
echo.
set "MKLINK="
set /p MKLINK="Create a desktop shortcut?  [Y/N] "
if /i "%MKLINK%"=="Y" (
    powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\myah.lnk'); $s.TargetPath='%~dp0run.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%SystemRoot%\System32\shell32.dll,14'; $s.Save()"
    echo Desktop shortcut created.
)

echo.
echo ============================================
echo    Done.  Launch myah with  run.bat
echo ============================================
pause
