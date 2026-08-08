@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ==================================================================
echo  ComfyLocal - start
echo ==================================================================

REM 1) Python
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set "PY=python"
  ) else (
    echo [CHYBA] Python 3.10+ neni nainstalovany.
    echo Nainstaluj ho z https://www.python.org/downloads/ a zaskrtni "Add python.exe to PATH".
    pause
    exit /b 1
  )
)

REM 2) Konfigurace
if not exist "config.json" (
  echo [INFO] config.json neexistuje - kopiruji z config.example.json
  copy /y "config.example.json" "config.json" >nul
  echo [INFO] Zkontroluj v config.json adresu comfy_url.
)

REM 3) Virtualni prostredi
if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Vytvarim virtualni prostredi .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [CHYBA] Nepodarilo se vytvorit .venv
    pause
    exit /b 1
  )
)

set "VENV_PY=.venv\Scripts\python.exe"

REM 4) Zavislosti. Znacka nese datum requirements.txt, takze po jeho zmene
REM    (nova verze balicku) se instalace zopakuje sama a nezustane stary stav.
set "REQ_STAMP="
for %%F in (requirements.txt) do set "REQ_STAMP=%%~tF"
set "NEED_INSTALL=1"
if exist ".venv\.deps_ok" (
  set /p DEPS_STAMP=<".venv\.deps_ok"
  if "!DEPS_STAMP!"=="!REQ_STAMP!" set "NEED_INSTALL=0"
)

if "!NEED_INSTALL!"=="1" (
  echo [INFO] Instaluji zavislosti ...
  "%VENV_PY%" -m pip install --upgrade pip >nul 2>nul
  "%VENV_PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [CHYBA] Instalace zavislosti selhala.
    echo Nejcastejsi duvod je firemni proxy nebo blokovany pristup na pypi.org.
    echo Zkus:  "%VENV_PY%" -m pip install -r requirements.txt
    pause
    exit /b 1
  )
  >".venv\.deps_ok" echo !REQ_STAMP!
)

REM 5) Start
echo [INFO] Spoustim ComfyLocal ... (ukonceni: Ctrl+C nebo zavreni okna)
echo [INFO] Log se zapisuje do data\logs\comfylocal.log
echo.
"%VENV_PY%" -m comfylocal
set "EXITCODE=%errorlevel%"
echo.
if not "%EXITCODE%"=="0" (
  echo [CHYBA] ComfyLocal skoncil s chybou %EXITCODE%.
  echo Cely vypis vcetne chyby najdes v souboru:  data\logs\comfylocal.log
) else (
  echo [INFO] ComfyLocal skoncil.
)
pause
