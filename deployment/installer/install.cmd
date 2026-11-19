@echo off
rem HCWMA installer: runs install.ps1 for this one run only (the PC's PowerShell policy is not changed).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
