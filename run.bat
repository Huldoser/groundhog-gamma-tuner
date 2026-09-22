@echo off
cd /d "%~dp0"
where pyw >nul 2>&1
if not errorlevel 1 (
  start "" pyw -3 "%~dp0main.py"
  exit /b 0
)
where pythonw >nul 2>&1
if not errorlevel 1 (
  start "" pythonw "%~dp0main.py"
  exit /b 0
)
echo Could not find Python. Install the 64-bit Python from python.org (Windows installer (64-bit)) and enable the py launcher.
pause
exit /b 1
