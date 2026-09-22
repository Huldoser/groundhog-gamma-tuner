@echo off
cd /d "%~dp0"
where cscript >nul 2>&1
if errorlevel 1 (
  echo Could not find cscript. Windows Script Host is required to create the shortcut.
  pause
  exit /b 1
)
cscript //nologo "%~dp0install-shortcut.vbs"
if errorlevel 1 (
  echo.
  echo Shortcut was not created.
  pause
  exit /b 1
)
echo.
pause
exit /b 0
