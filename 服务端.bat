@echo off
chcp 65001 >nul
rem Run as a headless server. Usage: server.bat [port]
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=9527
python -m lanbench serve --port %PORT%
pause
