@echo off
cd /d "%~dp0"
echo Building...
python -m PyInstaller --noconfirm app.spec
echo.
echo Done. Output is in the dist folder.
pause
