@echo off
rem One-click start for HCWMA: the window and the tray icon, no console window.
rem If HCWMA is already running, its window comes forward instead (only one may run).
cd /d "%~dp0..\.."
if not exist ".venv\Scripts\pythonw.exe" (
  echo HCWMA is not installed yet. Run deployment\installer\install.cmd first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m app.desktop
