@echo off
setlocal
cd /d "%~dp0"
title SC Portal 0.7

echo ==========================================
echo SC Portal 0.7 - Local Server
echo ==========================================

set "PYEXE="

rem 1) Python available in PATH
where python >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%P in ('where python') do (
    echo %%P | findstr /I "WindowsApps" >nul
    if errorlevel 1 if not defined PYEXE set "PYEXE=%%P"
  )
)

rem 2) Python Launcher for Windows
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYEXE=py"
)

rem 3) Common Anaconda locations used on workstation
if not defined PYEXE if exist "C:\ProgramData\Anaconda3\python.exe" set "PYEXE=C:\ProgramData\Anaconda3\python.exe"
if not defined PYEXE if exist "C:\Anaconda3\python.exe" set "PYEXE=C:\Anaconda3\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\Anaconda3\python.exe" set "PYEXE=%USERPROFILE%\Anaconda3\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\anaconda3\python.exe" set "PYEXE=%USERPROFILE%\anaconda3\python.exe"

if not defined PYEXE goto :NO_PYTHON

echo Python rilevato: %PYEXE%
echo Avvio SC Portal su http://localhost:8080
start "" "http://localhost:8080"

if /I "%PYEXE%"=="py" (
  py -m http.server 8080
) else (
  "%PYEXE%" -m http.server 8080
)

goto :END

:NO_PYTHON
echo.
echo ERRORE: non trovo un interprete Python utilizzabile.
echo.
echo SC Portal non richiede Python per funzionare: Python serve solo
 echo come piccolo server HTTP locale per testare la PWA.
echo.
echo Se usi Anaconda, apri Anaconda Prompt, entra in questa cartella e lancia:
echo     python -m http.server 8080
echo Poi apri http://localhost:8080

echo.
pause

:END
endlocal
