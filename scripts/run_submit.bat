@echo off
REM Hourly auto-submit wrapper.
REM
REM Invoked by Windows Task Scheduler. Runs the FTC submitter once,
REM which will file as many approved voicemails as donotcall.gov lets us
REM through in one session and then exit cleanly when it hits a
REM "system difficulties" throttle.
REM
REM All output (per-VM submission logs, errors, throttle notices) is
REM appended to logs/auto_submit.log. Each run is bracketed with a
REM timestamp banner so you can scroll back through history.

setlocal

REM Repo root = parent of scripts\ (edit if you move the install elsewhere).
set "REPO=%~dp0.."
set "REPO=%REPO:~0,-1%"
set PY=%REPO%\.venv\Scripts\python.exe
set LOGDIR=%REPO%\logs
set LOGFILE=%LOGDIR%\auto_submit.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM Skip if another submitter instance holds the lock (prevents two Chrome windows).
"%PY%" -c "import os,sys; from pathlib import Path; p=Path(r'%REPO%')/'ftc_automation'/'logs'/'submit.lock'; pid=int(p.read_text()) if p.exists() and p.read_text().strip().isdigit() else 0; alive=pid>0 and __import__('ctypes').windll.kernel32.OpenProcess(0x100000,0,pid) if pid else 0; sys.exit(1 if alive else 0)" >nul 2>&1
if %ERRORLEVEL%==1 (
    echo Skipped @ %date% %time% — submitter already running.>> "%LOGFILE%"
    exit /b 0
)

(
    echo.
    echo =====================================================
    echo  Auto-submit run @ %date% %time%
    echo =====================================================
    "%PY%" -m ftc_automation submit --once
    echo Exit code: %ERRORLEVEL%
) >> "%LOGFILE%" 2>&1

endlocal
