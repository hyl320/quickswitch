@echo off
rem One-click setup for running QuickSwitch from source.
rem Installs Python dependencies (pywin32), then starts the app.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/ first.
    pause
    exit /b 1
)

echo Installing dependencies...
python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

if exist "dist\QuickSwitch.exe" (
    start "" "dist\QuickSwitch.exe"
) else (
    start "" pythonw "QuickSwitch.pyw"
)
exit /b 0
