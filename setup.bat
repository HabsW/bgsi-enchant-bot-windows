@echo off
setlocal
cd /d "%~dp0"
echo === BGSI Enchant Bot - setup ===
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python was not found.
  echo Install Python 3.12 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during the install. Then run setup.bat again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating the .venv folder ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installing the Python packages failed - check your internet connection.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import sys, bgsi_enchanter.ocr as o; sys.exit(0 if o.tesseract_available() else 1)"
if errorlevel 1 (
  echo.
  echo Tesseract OCR ^(reads the enchant names^) is not installed.
  choice /M "Install it now with winget"
  if not errorlevel 2 (
    winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements
  ) else (
    echo Install it later from https://github.com/UB-Mannheim/tesseract/wiki
  )
)
echo.
".venv\Scripts\python.exe" -m bgsi_enchanter doctor
echo.
echo Setup finished. Start the bot with start.bat
pause
