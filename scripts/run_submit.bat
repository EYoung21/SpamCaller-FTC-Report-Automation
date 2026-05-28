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

set REPO=C:\Users\hello\Documents\FTCReportAutomation
set PY=%REPO%\.venv\Scripts\python.exe
set LOGDIR=%REPO%\logs
set LOGFILE=%LOGDIR%\auto_submit.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

(
    echo.
    echo =====================================================
    echo  Auto-submit run @ %date% %time%
    echo =====================================================
    "%PY%" -m ftc_automation submit --once
    echo Exit code: %ERRORLEVEL%
) >> "%LOGFILE%" 2>&1

endlocal
