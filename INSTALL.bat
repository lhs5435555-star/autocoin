@echo off
cd /d "%~dp0"
echo ============================================
echo   Installing dependencies...
echo ============================================
echo.
pip install -r fang_v10/requirements.txt
echo.
echo ============================================
echo   Done! Now double-click START.bat
echo ============================================
pause
