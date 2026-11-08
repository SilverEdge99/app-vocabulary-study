@echo off
rem ============================================================
rem  Vocab - LAN / phone access (same WiFi)
rem  Runs on port 80 when free, so the phone only needs http://<pc-ip>
rem  Prints the phone URL and the 8-char access code.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [x] Python not found. Install Python 3.9+ and check "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting in LAN mode (phone access)...
echo   Port 80 is used by default, so on the phone just open:  http://^<pc-ip^>
echo   Example:  http://192.168.2.4
echo   If port 80 is taken, the printed URL may include a port like :8017 - use that.
echo   Then type the 8-char access code shown below.
echo   Both devices must be on the same WiFi. Close this window to stop.
echo.
%PY% main.py --lan --port 80 %*

echo.
echo   Server stopped.
pause
