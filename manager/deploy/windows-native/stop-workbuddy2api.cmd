@echo off
REM WorkBuddy Manager - native stop script for upstream workbuddy2api (Windows).
REM
REM Companion to start-workbuddy2api.cmd (read that file first, including the
REM note about preferring the upstream-provided scripts when present).
REM
REM KEEP THIS FILE ASCII-ONLY: cmd.exe parses batch files in the system ANSI
REM codepage, so non-ASCII comments break the script. See start-workbuddy2api.cmd.
REM
REM HOW IT STOPS: by process name. The upstream is a single Go process with no
REM service registration, so taskkill is enough. This deliberately matches on
REM image name (/IM) rather than a PID file, because this template does not keep
REM one and image-name matching is reliable when only one instance runs.
REM
REM KNOWN TRADE-OFF: if two processes named wb2api.exe are running (e.g. started
REM from different directories), this kills both. The upstream-provided stop
REM script avoids that with a PID file + process-path check - prefer it when
REM available.

setlocal

REM Upstream process image name (must match WB2API_EXE in the start script)
set WB2API_IMAGE=wb2api.exe

REM Check whether it is running first. Exit 0 when it is not: stopping something
REM that was not running is not a failure, and returning non-zero here would make
REM the manager's "restart" fail on this first step.
tasklist /fi "IMAGENAME eq %WB2API_IMAGE%" 2>nul | find /i "%WB2API_IMAGE%" >nul
if errorlevel 1 (
    echo [stop] %WB2API_IMAGE% is not running, nothing to do
    exit /b 0
)

taskkill /f /im "%WB2API_IMAGE%" >nul 2>&1
if errorlevel 1 (
    echo [stop] failed to stop %WB2API_IMAGE% 1>&2
    exit /b 1
)

echo [stop] %WB2API_IMAGE% stopped
exit /b 0
