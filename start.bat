@echo off
rem ============================================================================
rem  PHANTOM DESK - launcher per Windows. Doppio clic per avviare.
rem  La prima volta crea l'ambiente Python e installa le dipendenze.
rem  Uso da terminale:  start.bat            avvia il desk (dashboard)
rem                     start.bat recap      recap delle ultime 4 ore
rem                     start.bat gate       il paper ha sbloccato il live?
rem                     start.bat wallet     verifica il wallet in .env
rem                     start.bat research   quali segnali hanno funzionato (dati shadow 60 min)
rem                     start.bat model      quanti dati di apprendimento ci sono + prova del modello
rem                     start.bat pages      pubblica ora la dashboard su GitHub Pages (bot avviato)
rem ============================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Phantom Desk

set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY goto nopython

if exist ".venv\Scripts\python.exe" goto venvok
echo Creo l'ambiente Python in .venv ...
%PY% -m venv .venv
if errorlevel 1 goto fail
:venvok
set "VPY=.venv\Scripts\python.exe"

fc /b requirements.txt .venv\installed-requirements.txt >nul 2>&1
if not errorlevel 1 goto depsok
echo Installo le dipendenze, solo la prima volta: 1-2 minuti ...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
copy /y requirements.txt .venv\installed-requirements.txt >nul
:depsok

if not exist ".env" copy ".env.example" ".env" >nul

"%VPY%" run.py %*
rem  Codice 75 = il desk ha chiesto di riavviarsi da solo (python run.py restart, dopo un git pull):
rem  riparte subito in questa finestra, ricontrollando anche le dipendenze.
if %errorlevel%==75 goto venvok
if errorlevel 1 pause
exit /b 0

:nopython
echo.
echo [!] Python 3.11+ non trovato.
echo     Installa Python 3.12 da https://www.python.org/downloads/
echo     spuntando "Add python.exe to PATH", poi riavvia questo file.
echo.
pause
exit /b 1

:fail
echo.
echo [!] Qualcosa e' andato storto: leggi il messaggio qui sopra.
echo.
pause
exit /b 1
