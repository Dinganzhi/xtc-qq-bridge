@echo off
REM =====================================================================
REM  XTC QQ Bridge - installer (Windows).  Pure ASCII on purpose:
REM  Chinese text / chcp 65001 in .bat files breaks cmd parsing.
REM  For Linux / macOS use install.sh instead.
REM =====================================================================
setlocal EnableExtensions
set "SRC=%~dp0"
set "DEST=%USERPROFILE%\.astrbot\data\plugins\xtc_qq_bridge"
cd /d "%SRC%"

echo ============================================
echo   XTC QQ Bridge - Installer (Windows)
echo ============================================
echo.

REM ---------- 1. Python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py"
if not defined PY (
    echo [ERROR] Python not found in PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [OK] %%v

REM ---------- 2. pyyaml ----------
"%PY%" -c "import yaml" >nul 2>&1
if not errorlevel 1 goto :yaml_ok
echo [..] Installing pyyaml (max 120s) ...
"%PY%" -c "import subprocess,sys; sys.exit(subprocess.run([sys.executable,'-m','pip','install','pyyaml'],timeout=120).returncode)"
"%PY%" -c "import yaml" >nul 2>&1
if not errorlevel 1 goto :yaml_ok
echo [WARN] pyyaml install failed - no network? Use JSON config or retry later.
goto :after_yaml

:yaml_ok
echo [OK] pyyaml present

:after_yaml

REM ---------- 3. Copy plugin to AstrBot ----------
if not exist "%DEST%" mkdir "%DEST%"
copy /Y "%SRC%astrbot_plugin_xtc_bridge\main.py"               "%DEST%\" >nul
copy /Y "%SRC%astrbot_plugin_xtc_bridge\metadata.yaml"         "%DEST%\" >nul
copy /Y "%SRC%astrbot_plugin_xtc_bridge\_conf_schema.json"     "%DEST%\" >nul
copy /Y "%SRC%astrbot_plugin_xtc_bridge\README.md"             "%DEST%\" >nul
echo [OK] Plugin copied to %DEST%

REM ---------- 4. Initial plugin config (only if missing) ----------
set "PCFG=%USERPROFILE%\.astrbot\data\config\xtc_qq_bridge_config.json"
if exist "%PCFG%" goto :pcfg_ok
if not exist "%USERPROFILE%\.astrbot\data\config" mkdir "%USERPROFILE%\.astrbot\data\config"
copy /Y "%SRC%astrbot_plugin_xtc_bridge\plugin_config.example.json" "%PCFG%" >nul
echo [OK] Plugin config initialized: %PCFG%
goto :cfg

:pcfg_ok
echo [OK] Plugin config already exists - keep as-is

:cfg

REM ---------- 5. Bridge config.yaml (only if missing) ----------
if exist "%SRC%config.yaml" goto :cfg_exists
copy /Y "%SRC%config.example.yaml" "%SRC%config.yaml" >nul
echo [OK] config.yaml created from template - EDIT IT before running
goto :apk

:cfg_exists
echo [OK] config.yaml exists - keep as-is

:apk

REM ---------- 6. ADBKeyBoard APK (local file only, no download) ----------
if exist "%SRC%keyboardservice-debug.apk" goto :apk_ok
if exist "%SRC%ADBKeyBoard.apk" goto :apk_ok
echo [WARN] No ADBKeyBoard APK in the project folder.
echo        Ignore if the device already has ADBKeyBoard; otherwise Chinese
echo        text cannot be typed - put the APK back into the project root.
goto :next

:apk_ok
echo [OK] ADBKeyBoard APK ready - installed to the device only if missing

:next
echo.
echo ============================================
echo   NEXT STEPS
echo   1. Edit config.yaml: QQ numbers, contact,
echo      nicknames, tokens (match plugin config)
echo   2. Start AstrBot once, enable plugin
echo      "xtc_qq_bridge" in WebUI
echo   3. Configure NapCat adapter, login QQ bot
echo   4. Send the bot a message once
echo      (learns platform id)
echo   5. Double-click start.bat  (or: python main.py)
echo ============================================
pause
