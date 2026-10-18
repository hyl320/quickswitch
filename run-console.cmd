@echo off
rem Start QuickSwitch with a console window so that errors are visible.
rem For normal use, double click QuickSwitch.vbs instead.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python "QuickSwitch.pyw" %*
) else if exist "D:\py\python.exe" (
    "D:\py\python.exe" "QuickSwitch.pyw" %*
) else (
    echo Python not found. Please install Python 3.10 or newer.
    pause
)
