@echo off
chcp 65001 >nul

rem 记住用户运行目录（启动服务后作为 project_root），避免 cd 后丢失
set "USER_ROOT=%cd%"
cd /d "%~dp0"

::: Check venv
if not exist "..\.venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

::: Read config
set BACKEND=native
set CLI_NAME=codebuddy
if exist "%cd%\cli_config.json" (
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content '%cd%\cli_config.json' -Encoding UTF8 | ConvertFrom-Json).backend" 2^>nul') do set BACKEND=%%i
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content '%cd%\cli_config.json' -Encoding UTF8 | ConvertFrom-Json).cli" 2^>nul') do set CLI_NAME=%%i
)
if "%BACKEND%"=="" set BACKEND=native
if "%CLI_NAME%"=="" set CLI_NAME=codebuddy

::: 多开支持：不杀旧进程。8420 被占用时 main.py 自动分配新端口（8421/8422...）
echo Agent OS supports multiple instances (auto port allocation). Existing instances are kept.

echo.
echo ========================================
echo   Agent OS
echo   Backend: %BACKEND%
echo   CLI:     %CLI_NAME%
echo ========================================
echo.

::: Activate venv and start
call ..\.venv\Scripts\activate.bat
set AGENT_OS_BACKEND=%BACKEND%
python main.py --root "%USER_ROOT%" --cli "%CLI_NAME%" --backend "%BACKEND%"
pause
