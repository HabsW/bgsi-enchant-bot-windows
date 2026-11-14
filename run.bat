@echo off
rem Console commands: run.bat run | analyze | doctor | mousetest | probe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call setup.bat
".venv\Scripts\python.exe" -m bgsi_enchanter %*
