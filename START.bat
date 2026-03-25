@echo off
cd /d "%~dp0"
echo ============================================
echo   FANG SCALPER v10 Starting...
echo ============================================
echo.
python fang_v10/run_dashboard.py
if %errorlevel% neq 0 (
    echo.
    echo ============================================
    echo   ERROR - Please check:
    echo   1. Python installed? (python.org)
    echo   2. Run: pip install -r fang_v10/requirements.txt
    echo ============================================
    pause
)
