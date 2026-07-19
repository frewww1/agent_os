@echo off
chcp 65001 >nul

cd /d "%~dp0"

:: Check venv
if not exist "..\.venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Read config
set BACKEND=native
set CLI_NAME=codebuddy
if exist "%cd%\cli_config.json" (
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content '%cd%\cli_config.json' -Encoding UTF8 | ConvertFrom-Json).backend" 2^>nul') do set BACKEND=%%i
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content '%cd%\cli_config.json' -Encoding UTF8 | ConvertFrom-Json).cli" 2^>nul') do set CLI_NAME=%%i
)
if "%BACKEND%"=="" set BACKEND=native
if "%CLI_NAME%"=="" set CLI_NAME=codebuddy

:: Kill existing instance on port 8420
echo Stopping any existing Agent OS...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8420.*LISTENING"') do taskkill /pid %%a /f /t 2>nul
timeout /t 2 /nobreak >nul

echo.
echo ========================================
echo   Agent OS
echo   Backend: %BACKEND%
echo   CLI:     %CLI_NAME%
echo ========================================
echo.

:: Activate venv and start
call ..\.venv\Scripts\activate.bat
set AGENT_OS_BACKEND=%BACKEND%
python main.py --cli "%CLI_NAME%" --backend "%BACKEND%"
pause
