@echo off
chcp 65001 >nul
echo ============================================
echo   FANG SCALPER v10 시작중...
echo ============================================
echo.

cd /d "%~dp0"
python fang_v10/run_dashboard.py

if %errorlevel% neq 0 (
    echo.
    echo ============================================
    echo   오류가 발생했습니다.
    echo   Python이 설치되어 있는지 확인하세요.
    echo   pip install -r fang_v10/requirements.txt
    echo ============================================
    pause
)
