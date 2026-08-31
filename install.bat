@echo off
chcp 65001 >nul
setlocal
set "SRC=%~dp0"
set "DEST=%USERPROFILE%\.astrbot\data\plugins\xtc_qq_bridge"

echo ============================================
echo   XTC QQ Bridge - Installer
echo ============================================

REM ---------- 1. Python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo [OK] %%v

REM ---------- 2. pyyaml ----------
python -c "import yaml" >nul 2>&1
if errorlevel 1 (
    echo [..] Installing pyyaml ...
    pip install pyyaml
    if errorlevel 1 (
        echo [WARN] pyyaml install failed (no network?). Use JSON config or retry later.
    ) else (
        echo [OK] pyyaml installed
    )
) else (
    echo [OK] pyyaml present
)

REM ---------- 3. Copy plugin to AstrBot ----------
if not exist "%DEST%" mkdir "%DEST%"
copy /Y "%SRC%astrbot_plugin_xtc_bridge\main.py"         "%DEST%\" >nul
copy /Y "%SRC%astrbot_plugin_xtc_bridge\metadata.yaml"   "%DEST%\" >nul
copy /Y "%SRC%astrbot_plugin_xtc_bridge\_conf_schema.json" "%DEST%\" >nul
copy /Y "%SRC%astrbot_plugin_xtc_bridge\README.md"       "%DEST%\" >nul
echo [OK] Plugin copied to %DEST%

REM ---------- 4. Initial plugin config (only if missing) ----------
set "PCFG=%USERPROFILE%\.astrbot\data\config\xtc_qq_bridge_config.json"
if not exist "%PCFG%" (
    if not exist "%USERPROFILE%\.astrbot\data\config" mkdir "%USERPROFILE%\.astrbot\data\config"
    copy /Y "%SRC%astrbot_plugin_xtc_bridge\plugin_config.example.json" "%PCFG%" >nul
    echo [OK] Plugin config initialized: %PCFG%
) else (
    echo [OK] Plugin config already exists (keep as-is)
)

REM ---------- 5. Bridge config.yaml (only if missing) ----------
if not exist "%SRC%config.yaml" (
    copy /Y "%SRC%config.example.yaml" "%SRC%config.yaml" >nul
    echo [OK] config.yaml created from template - EDIT IT before running!
) else (
    echo [OK] config.yaml exists (keep as-is)
)

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
echo   5. python main.py
echo ============================================
pause
