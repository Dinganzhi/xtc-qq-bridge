@echo off
REM =====================================================================
REM  Build machine-code single-file executables with Nuitka (Windows).
REM  Pure ASCII on purpose (Chinese text breaks cmd parsing).
REM
REM  Usage:
REM    build.bat                     onefile, both targets, output dist\
REM    build.bat --mode standalone   folder mode (faster startup)
REM    build.bat --target bridge     only the bridge
REM    build.bat --check-env         check Python/pyyaml/Nuitka/MSVC
REM    build.bat --dry-run           print the nuitka command lines only
REM =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Build XTC QQ Bridge

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
echo   Build with Nuitka  (Windows)
echo ============================================
echo [1/2] Checking build environment ...
"%PY%" tools\build_nuitka.py --check-env
if errorlevel 1 (
  echo.
  echo [hint] Need: pip install -U nuitka pyyaml  +  Visual Studio 2022 with
  echo        "Desktop development with C++"  ^(MinGW64 does NOT support Python 3.13+^).
  echo.
  pause
  exit /b 1
)

echo.
echo [2/2] Building ...
"%PY%" tools\build_nuitka.py %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [exit] build failed, code %RC%
) else (
  echo [exit] build ok - see the dist folder
)
pause
endlocal
exit /b %RC%
