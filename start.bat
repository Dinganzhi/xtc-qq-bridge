@echo off
REM =====================================================================
REM  XTC QQ Bridge - launcher (Windows).  Pure ASCII on purpose:
REM  Chinese text / chcp 65001 in .bat files breaks cmd parsing and
REM  shows "command not found" errors, so keep this file English only.
REM =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title XTC QQ Bridge

REM Force UTF-8 for Python only (Chinese config/logs need it). This does
REM NOT change the console code page, so .bat parsing stays safe.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================
echo   XTC QQ Bridge - Launcher
echo ============================================
echo.

REM ---------- 0. Python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py"
if not defined PY goto :no_python

for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [env] %%v
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :old_python

REM ---------- 1. pyyaml (best effort, never blocks) ----------
"%PY%" -c "import yaml" >nul 2>&1
if not errorlevel 1 goto :yaml_ok
echo [dep] pyyaml missing, trying to install (max 120s) ...
REM timeout guard: a blocked/no-network pip must never hang the launcher
"%PY%" -c "import subprocess,sys; sys.exit(subprocess.run([sys.executable,'-m','pip','install','pyyaml'],timeout=120).returncode)" >nul 2>&1
"%PY%" -c "import yaml" >nul 2>&1
if not errorlevel 1 goto :yaml_ok
echo [warn] pyyaml install failed - use JSON config or install it manually.
goto :after_yaml

:yaml_ok
echo [dep] pyyaml ok

:after_yaml

REM ---------- 2. config.yaml ----------
if exist "config.yaml" goto :cfg_ok
if not exist "config.example.yaml" goto :no_template
copy /Y "config.example.yaml" "config.yaml" >nul
echo [cfg] config.yaml created from config.example.yaml
echo.
echo [NOTE] Edit config.yaml first. At least fill in:
echo        target.qq_private / target.qq_group, target.xtc_contact,
echo        forward.plugin.token, webhook.token / allow_from
echo        (must match the AstrBot plugin config)
echo        adb.port / adb.serial if auto-detection picks the wrong device
echo.
echo Open config.yaml in Notepad now? (Y/N)
set "OPENCFG="
set /p "OPENCFG="
if /i not "%OPENCFG%"=="Y" goto :cfg_hint
start "" notepad "config.yaml"
:cfg_hint
echo Re-run this script after editing config.yaml.
goto :end

:cfg_ok
echo [cfg] config.yaml found
goto :cfg_done

:no_template
echo [error] Neither config.yaml nor config.example.yaml found.
goto :end

:cfg_done

REM ---------- 3. ADBKeyBoard APK (local file only, never downloaded) ----------
if exist "keyboardservice-debug.apk" goto :apk_ok
if exist "ADBKeyBoard.apk" goto :apk_ok
echo [warn] No ADBKeyBoard APK in this folder
echo        (keyboardservice-debug.apk / ADBKeyBoard.apk).
echo        Ignore if the device already has ADBKeyBoard, otherwise
echo        Chinese text cannot be typed.
:apk_ok

REM ---------- 4. Pass-through: start.bat --check / --once / --debug adb-info ----------
if "%~1"=="" goto :menu
echo [run] main.py %*
echo.
"%PY%" main.py %*
set "APP_RC=1"
if not errorlevel 1 set "APP_RC=0"
echo.
echo [exit] code %APP_RC%
goto :end

:menu
echo.
echo --------------------------------------------
echo   1. Start bridge            (python main.py)
echo   2. Environment check       (--check)
echo   3. Single poll then exit   (--once)
echo   4. Dump current UI         (--debug dump-ui)
echo   5. Edit config.yaml
echo   6. Show log tail           (logs\bridge.log)
echo   0. Quit
echo --------------------------------------------
set "CH="
set /p "CH=Select [1]: "
if not defined CH set "CH=1"
if "%CH%"=="1" goto :run
if "%CH%"=="2" goto :check
if "%CH%"=="3" goto :once
if "%CH%"=="4" goto :dumpui
if "%CH%"=="5" goto :opencfg
if "%CH%"=="6" goto :viewlog
if "%CH%"=="0" goto :end
echo [warn] Invalid choice, try again.
goto :menu

:run
echo.
echo [run] Starting bridge (Ctrl+C to stop)
echo.
"%PY%" main.py
set "APP_RC=1"
if not errorlevel 1 set "APP_RC=0"
echo.
echo [exit] code %APP_RC%
if not "%APP_RC%"=="0" echo [hint] Start failed - run option 2 first.
goto :end

:check
echo.
"%PY%" main.py --check
echo.
pause
goto :menu

:once
echo.
"%PY%" main.py --once
echo.
pause
goto :menu

:dumpui
echo.
"%PY%" main.py --debug dump-ui
echo.
pause
goto :menu

:opencfg
start "" notepad "config.yaml"
goto :menu

:viewlog
if exist "logs\bridge.log" goto :log_tail
echo [warn] logs\bridge.log not found yet (start the bridge once).
echo.
pause
goto :menu

:log_tail
echo.
powershell -NoProfile -Command "Get-Content -Path 'logs\bridge.log' -Tail 40"
echo.
pause
goto :menu

:no_python
echo [error] Python not found. Install Python 3.10+ and enable "Add to PATH".
echo         https://www.python.org/downloads/
goto :end

:old_python
echo [error] Python is too old. Version 3.10 or newer is required.
goto :end

:end
echo.
pause
endlocal
