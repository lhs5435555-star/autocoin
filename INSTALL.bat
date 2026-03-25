@echo off
cd /d "%~dp0"
echo ============================================
echo   Installing dependencies...
echo ============================================
echo.
py -m pip install PyQt5 ccxt pandas matplotlib
echo.
echo ============================================
echo   Done! Now double-click START.bat
echo ============================================
pause
