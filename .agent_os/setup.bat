@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ============================================================
:: Agent OS Setup Script (Windows)
:: ============================================================

cd /d "%~dp0"

echo.
echo ========================================
echo   Agent OS - Environment Setup
echo ========================================
echo.

:: ============================================================
:: 1. Python
:: ============================================================
echo [1/5] Checking Python...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python not found. Install Python 3.10+ from https://www.python.org/
    echo           Check "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PY_VER%") do (set MAJOR=%%a & set MINOR=%%b)
if %MAJOR% lss 3 (
    echo   [ERROR] Python 3.10+ required, got %PY_VER%
    pause
    exit /b 1
)
if %MAJOR% equ 3 if %MINOR% lss 10 (
    echo   [ERROR] Python 3.10+ required, got %PY_VER%
    pause
    exit /b 1
)
echo   [OK] Python %PY_VER%

:: ============================================================
:: 2. Git
:: ============================================================
echo [2/5] Checking Git...
where git >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Git not found. Install from https://git-scm.com/
    pause
    exit /b 1
)
for /f "tokens=3" %%v in ('git --version 2^>^&1') do echo   [OK] Git %%v

:: ============================================================
:: 3. CodeBuddy CLI
:: ============================================================
echo [3/5] Checking CodeBuddy CLI...
where codebuddy >nul 2>&1
if %errorlevel% neq 0 (
    echo   CodeBuddy CLI not found. Installing...
    call npm install -g @anthropic-ai/codebuddy-cli 2>nul
    if !errorlevel! neq 0 (
        echo   [ERROR] Failed to install CodeBuddy CLI.
        echo           Try: npm install -g @anthropic-ai/codebuddy-cli
        pause
        exit /b 1
    )
    echo   [OK] CodeBuddy CLI installed
) else (
    echo   [OK] CodeBuddy CLI found
)

:: ============================================================
:: 4. Virtual Env + Dependencies
:: ============================================================
echo [4/5] Setting up Python environment...

set VENV_DIR=%cd%\..\.venv

if not exist "%VENV_DIR%\Scripts\python.exe" (
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo   Virtual environment created.
)

call "%VENV_DIR%\Scripts\activate.bat"

if exist "%cd%\requirements.txt" (
    echo   Installing dependencies...
    pip install -r "%cd%\requirements.txt" --quiet
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo   [OK] Dependencies installed.
)

:: ============================================================
:: 5. Config
:: ============================================================
echo [5/5] Writing config...

:: Choose backend
echo.
echo   Backend:
echo   [1] CLI mode (subprocess, default)
echo   [2] SDK mode (codebuddy-agent-sdk, faster)
echo.
set /p BE_CHOICE="   Choose (1 or 2, default=1): "
if "%BE_CHOICE%"=="" set BE_CHOICE=1

set BACKEND=native
if "%BE_CHOICE%"=="2" set BACKEND=codebuddy-sdk

echo {"cli": "codebuddy", "backend": "%BACKEND%"} > "%cd%\cli_config.json"
echo   Config saved: .agent_os\cli_config.json

:: ============================================================
echo.
echo ========================================
echo   Setup complete!
echo.
echo   Backend: %BACKEND%
echo.
echo   Start Agent OS:
echo     .agent_os\start.bat
echo.
echo   Or manually:
echo     .venv\Scripts\activate
echo     python .agent_os\main.py
echo ========================================
echo.

endlocal
pause
