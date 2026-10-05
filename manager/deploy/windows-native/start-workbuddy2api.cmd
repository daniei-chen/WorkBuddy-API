@echo off
REM WorkBuddy Manager - native start script for upstream workbuddy2api (Windows).
REM
REM WHEN THIS IS NEEDED: when the upstream runs as a local process instead of a
REM Docker container, the manager needs a start/stop script pair, pointed at by
REM WB2API_START_SCRIPT / WB2API_STOP_SCRIPT in .env.
REM
REM PREFER THE UPSTREAM ONE: workbuddy2api has shipped its own
REM start/stop/status-workbuddy2api.cmd since 2026-09-18. Those are better (they
REM track a PID file and verify the process path, so they never kill a same-named
REM process by mistake; the status script also probes /healthz). Use them if your
REM upstream directory has them - this file is only for OLDER upstream releases.
REM
REM WHY THIS FILE IS ASCII-ONLY: cmd.exe parses batch files using the system ANSI
REM codepage (936/GBK on Chinese Windows, 437 on US Windows). Non-ASCII bytes
REM (e.g. UTF-8 Chinese comments) get mangled, the REM lines stop being treated
REM as comments, and the fragments are executed as commands. The script then
REM breaks on exactly the machines it is meant for. Keep this file ASCII.
REM
REM USAGE:
REM   1) Copy next to your upstream build, fix the two TODOs below.
REM   2) In .env:
REM        WB2API_MODE=native
REM        WB2API_START_SCRIPT=C:/path/to/workbuddy2api/start-workbuddy2api.cmd
REM        WB2API_STOP_SCRIPT=C:/path/to/workbuddy2api/stop-workbuddy2api.cmd
REM        WB2API_LOG_FILE=C:/path/to/workbuddy2api/data/server.err.log
REM   3) Saving settings in the manager will now restart the upstream via these.
REM
REM REQUIREMENTS:
REM   - Must return IMMEDIATELY (the manager waits for the script to exit).
REM     Launch in the background; never run the server in the foreground here,
REM     or "restart" will hang until it times out.
REM   - Upstream stdout/stderr should go to WB2API_LOG_FILE - the manager's
REM     task log page reads its automatic-task output from that file.

setlocal
cd /d "%~dp0"

REM TODO 1: path to the upstream binary
REM   (build it from upstream source with: go build -o wb2api.exe ./cmd/server)
set WB2API_EXE=%~dp0wb2api.exe

REM TODO 2: upstream config file and data directory
set WB2API_CONFIG=%~dp0config.json
set WB2API_DATADIR=%~dp0data

if not exist "%WB2API_EXE%" (
    echo [start] upstream binary not found: %WB2API_EXE% 1>&2
    echo [start] build it first: go build -o wb2api.exe ./cmd/server 1>&2
    exit /b 1
)
if not exist "%WB2API_DATADIR%" mkdir "%WB2API_DATADIR%"

REM `start /b` launches in the background and returns immediately.
REM Logs are redirected to files for the manager to read.
start "workbuddy2api" /b "%WB2API_EXE%" -config "%WB2API_CONFIG%" >> "%WB2API_DATADIR%\server.out.log" 2>> "%WB2API_DATADIR%\server.err.log"

echo [start] workbuddy2api launched
exit /b 0
