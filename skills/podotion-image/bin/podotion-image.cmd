@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0podotion-image.ps1" %*
exit /b %ERRORLEVEL%
