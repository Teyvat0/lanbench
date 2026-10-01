@echo off
chcp 65001 >nul
rem Run the loopback self test (13 assertions).
cd /d "%~dp0"
python selftest.py %*
pause
