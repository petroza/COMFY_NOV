@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (
  where python >nul 2>nul
  if %errorlevel%==0 (set "PY=python") else (
    echo [CHYBA] Neni nainstalovany Python 3.10 nebo novejsi.
    pause
    exit /b 1
  )
)
%PY% INSTALL.py
if errorlevel 1 (
  echo [CHYBA] Prvni nastaveni se nepovedlo.
  pause
  exit /b 1
)
call START_WINDOWS.bat
