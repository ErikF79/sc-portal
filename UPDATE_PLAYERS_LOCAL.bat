@echo off
setlocal
rem Aggiorna le statistiche giocatori PlayBasket dal PC locale (la CI riceve 403).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update-players-local.ps1"
pause
exit /b %ERRORLEVEL%
