@echo off
chcp 65001 >nul
rem OPTIONAL: build a standalone exe (no Python needed on target).
rem Downloads/installs PyInstaller the first time (needs network).
cd /d "%~dp0"

python -c "import PyInstaller" 2>nul
if not "%ERRORLEVEL%"=="0" (
  echo PyInstaller not found, installing...
  python -m pip install --upgrade pyinstaller || goto fail
)

python -m PyInstaller --noconfirm --clean --onefile --windowed --name LANBench-GUI lanbench.py || goto fail
python -m PyInstaller --noconfirm --clean --onefile --console  --name LANBench-Serve lanbench.py || goto fail

echo.
echo Done. Files are in dist\
echo   dist\LANBench-GUI.exe    --^> double click for the GUI
echo   dist\LANBench-Serve.exe  --^> LANBench-Serve.exe serve --port 9527
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
