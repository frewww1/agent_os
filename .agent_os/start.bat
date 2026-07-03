@echo off
chcp 65001 >nul

cd /d "%~dp0"

if not exist "..\.venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Read CLI choice from config
set CLI=codebuddy
if exist "%cd%\cli_config.json" (
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content '%cd%\cli_config.json' -Encoding UTF8 | ConvertFrom-Json).cli" 2^>nul') do set CLI=%%i
)
echo Starting Agent OS with CLI: %CLI%
echo.

call ..\.venv\Scripts\activate.bat
python main.py --cli %CLI%
pause
