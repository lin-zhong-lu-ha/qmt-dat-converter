@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_converter.ps1" %*
exit /b %ERRORLEVEL%
