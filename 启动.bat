@echo off
rem ============================================================
rem  Vocab - local offline vocabulary trainer
rem  default: http://127.0.0.1:8017  (localhost only)
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py"
%PY% --version >nul 2>nul
if errorlevel 1 goto nopython

echo.
echo   Vocab is starting...
echo   Browser will open: http://127.0.0.1:8017
echo   Close this window to stop the server.
echo.
%PY% main.py %*

echo.
echo   Server stopped.
pause
exit /b 0

:nopython
echo.
echo   [x] Python not found.
echo       Install Python 3.9+ and check "Add python.exe to PATH".
echo.
pause
exit /b 1
