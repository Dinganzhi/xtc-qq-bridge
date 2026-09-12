@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title XTC QQ Bridge
REM Force UTF-8 for Python: Chinese Windows defaults to cp936 and mangles config/logs
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ============================================
echo   小天才 ^<-^> QQ 桥接   启动器
echo   XTC QQ Bridge Launcher
echo ============================================
echo.

REM ---------- 0. 探测 Python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY where py >nul 2>&1 && set "PY=py"
if not defined PY goto :no_python

for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [环境] %%v
"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :old_python

REM ---------- 1. pyyaml（缺失时尝试安装，失败不阻塞） ----------
"%PY%" -c "import yaml" >nul 2>&1
if not errorlevel 1 goto :yaml_ok
echo [依赖] 缺少 pyyaml，尝试安装...
"%PY%" -m pip install pyyaml >nul 2>&1
"%PY%" -c "import yaml" >nul 2>&1
if not errorlevel 1 goto :yaml_ok
echo [警告] pyyaml 安装失败（可能没网）。可改用 JSON 格式的 config.yaml。
goto :after_yaml

:yaml_ok
echo [依赖] pyyaml 已就绪

:after_yaml

REM ---------- 2. config.yaml ----------
if exist "config.yaml" goto :cfg_ok
if not exist "config.example.yaml" goto :no_template
copy /Y "config.example.yaml" "config.yaml" >nul
echo [配置] 已从模板生成 config.yaml
echo.
echo [注意] 请先编辑 config.yaml，至少填写：
echo        target.qq_private / qq_group、target.xtc_contact、
echo        forward.plugin.token、webhook.token / allow_from（与插件配置一致）
echo        以及 WSA 用户：adb.wsa_port 见 README 的 WSA 章节
echo.
echo        现在打开 config.yaml 吗？  Y 打开，直接回车 = 跳过
set "OPENCFG="
set /p "OPENCFG="
if /i not "%OPENCFG%"=="Y" goto :cfg_hint
start "" notepad "config.yaml"
:cfg_hint
echo 填好后重新运行本脚本即可。
goto :end

:cfg_ok
echo [配置] config.yaml 已就绪
goto :cfg_done

:no_template
echo [错误] 找不到 config.yaml 与 config.example.yaml。
goto :end

:cfg_done

REM ---------- 3. ADBKeyBoard 本地 APK（仅提示，不联网下载） ----------
if exist "keyboardservice-debug.apk" goto :apk_ok
if exist "ADBKeyBoard.apk" goto :apk_ok
echo [警告] 项目目录里没有 ADBKeyBoard 的 APK
echo        ^(keyboardservice-debug.apk / ADBKeyBoard.apk^)。
echo        若目标设备上已安装 ADBKeyBoard 可忽略；否则中文将无法输入。
:apk_ok

REM ---------- 4. 参数透传：start.bat --check / --once / --debug adb-info ----------
if "%~1"=="" goto :menu
echo [运行] main.py %*
echo.
"%PY%" main.py %*
set "APP_RC=1"
if not errorlevel 1 set "APP_RC=0"
echo.
echo [退出] 返回码 %APP_RC%
goto :end

:menu
echo.
echo --------------------------------------------
echo   1. 启动桥接          ^(python main.py^)
echo   2. 环境自检          ^(adb/连接/WSA/输入法/剪贴板^)
echo   3. 干跑一轮          ^(--once，读一轮后退出^)
echo   4. 打印当前界面控件  ^(--debug dump-ui^)
echo   5. 打开 config.yaml
echo   6. 查看日志          ^(logs\bridge.log 末尾^)
echo   0. 退出
echo --------------------------------------------
set "CH="
set /p "CH=请选择 [1]: "
if not defined CH set "CH=1"
if "%CH%"=="1" goto :run
if "%CH%"=="2" goto :check
if "%CH%"=="3" goto :once
if "%CH%"=="4" goto :dumpui
if "%CH%"=="5" goto :opencfg
if "%CH%"=="6" goto :viewlog
if "%CH%"=="0" goto :end
echo [提示] 无效选择，请重新输入。
goto :menu

:run
echo.
echo [运行] 启动桥接（Ctrl+C 退出）
echo.
"%PY%" main.py
set "APP_RC=1"
if not errorlevel 1 set "APP_RC=0"
echo.
echo [退出] 返回码 %APP_RC%
if not "%APP_RC%"=="0" echo [提示] 启动失败，建议先选 2 做一次环境自检。
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
echo [提示] 还没有日志文件 logs\bridge.log（先启动一次桥接）。
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
echo [错误] 找不到 Python。请先安装 Python 3.10+ 并勾选 "Add to PATH"。
echo        下载: https://www.python.org/downloads/
goto :end

:old_python
echo [错误] Python 版本过低，需要 3.10 及以上。
goto :end

:end
echo.
pause
endlocal
