@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Import-AK.ps1" -Pause
exit /b %errorlevel%
