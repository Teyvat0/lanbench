@echo off
chcp 65001 >nul
rem Open the GUI (no console window). Requires Python 3.8+ with tcl/tk.
rem NOTE: keep this file ASCII-only (see the build script comment for why).
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH. Install Python 3.8+ and tick "tcl/tk" during setup.
  pause
  exit /b 1
)
start "" pythonw -m lanbench gui
