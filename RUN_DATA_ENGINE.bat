@echo off
setlocal
cd /d "%~dp0data_engine"
title SC Portal Data Engine 0.7
set "PYEXE="
where python >nul 2>nul
if not errorlevel 1 for /f "delims=" %%P in ('where python') do (echo %%P | findstr /I "WindowsApps" >nul || if not defined PYEXE set "PYEXE=%%P")
if not defined PYEXE where py >nul 2>nul && set "PYEXE=py"
if not defined PYEXE if exist "C:\ProgramData\Anaconda3\python.exe" set "PYEXE=C:\ProgramData\Anaconda3\python.exe"
if not defined PYEXE if exist "C:\Anaconda3\python.exe" set "PYEXE=C:\Anaconda3\python.exe"
if not defined PYEXE goto NO_PY
if /I "%PYEXE%"=="py" (py sc_data_engine.py) else ("%PYEXE%" sc_data_engine.py)
pause
exit /b
:NO_PY
echo Python non trovato.
pause
