@echo off
REM =====================================================================
REM  WSA / WSABuilds network guard launcher (Windows).
REM  Kept pure ASCII on purpose: Chinese text in .bat files breaks cmd.
REM  Double-click this file to keep a resident guard window open.
REM =====================================================================
setlocal EnableExtensions
cd /d "%~dp0.."
title WSA Network Guard

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py"
if not defined PY (
  echo [error] Python not found. Install Python 3.10+ and enable "Add to PATH".
  pause
  exit /b 1
)

echo ============================================
echo   WSA / WSABuilds Network Guard
echo ============================================
echo   Repairs WSA network drops in tiers:
echo     reconnect -^> kill/start adb server -^> net reset
echo     -^> reboot subsystem (cooldown) -^> restart WSA (optional)
echo   Ctrl+C to stop.
echo.

"%PY%" tools\wsa_net_guard.py %*

echo.
echo [exit] guard stopped. Log: logs\wsa_guard.log
pause
endlocal
