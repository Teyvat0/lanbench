@echo off
chcp 65001 >nul
rem Build single-file LANBench.pyz (target machine still needs Python 3.8+).
rem NOTE: keep this file ASCII-only -- cmd.exe reads .bat with the OEM codepage,
rem       and non-ASCII bytes can shift/split the following command lines.
cd /d "%~dp0"

if exist _pkg rmdir /s /q _pkg
mkdir _pkg
xcopy /e /i /q lanbench _pkg\lanbench >nul
rem 别把 __pycache__ 里的 .pyc 打进包里（体积翻倍且没意义）
if exist _pkg\lanbench\__pycache__ rmdir /s /q _pkg\lanbench\__pycache__
if exist selftest.py copy /y selftest.py _pkg\ >nul
if exist README.md copy /y README.md _pkg\ >nul
if exist LICENSE copy /y LICENSE _pkg\ >nul
if exist LANBench.pyz del /q LANBench.pyz

python -m zipapp _pkg -m "lanbench.cli:main" -o LANBench.pyz -c
set RC=%ERRORLEVEL%
rmdir /s /q _pkg
if not "%RC%"=="0" goto fail
if not exist LANBench.pyz goto fail
echo.
echo Built: LANBench.pyz
echo   python LANBench.pyz          --^> GUI
echo   python LANBench.pyz serve    --^> server only
echo   python LANBench.pyz test IP  --^> command line speed test
pause
exit /b 0

:fail
echo Build failed, exit code %RC%
pause
exit /b 1
