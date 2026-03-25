@echo off
cd /d "%~dp0"
echo ============================================
echo   FANG SCALPER v10 Starting...
echo ============================================
echo.

if exist "fang_v10\run_dashboard.py" (
    py fang_v10\run_dashboard.py
) else if exist "run_dashboard.py" (
    py run_dashboard.py
) else (
    echo ERROR: run_dashboard.py not found
    pause
)

if %errorlevel% neq 0 (
    echo.
    echo   ERROR - Run INSTALL.bat first
    pause
)
