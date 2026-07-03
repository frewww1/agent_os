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
echo [1/6] Checking Python...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python not found. Install Python 3.8+ from https://www.python.org/
    echo           Check "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PY_VER%") do (set MAJOR=%%a & set MINOR=%%b)
if %MAJOR% lss 3 (
    echo   [ERROR] Python 3.8+ required, got %PY_VER%
    pause
    exit /b 1
)
if %MAJOR% equ 3 if %MINOR% lss 8 (
    echo   [ERROR] Python 3.8+ required, got %PY_VER%
    pause
    exit /b 1
)
echo   [OK] Python %PY_VER%

:: ============================================================
:: 2. Git
:: ============================================================
echo [2/6] Checking Git...
where git >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Git not found. Install from https://git-scm.com/
    pause
    exit /b 1
)
for /f "tokens=3" %%v in ('git --version 2^>^&1') do echo   [OK] Git %%v

:: ============================================================
:: 3. Node.js / npm
:: ============================================================
echo [3/6] Checking Node.js / npm...
where npm >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Node.js not found. Install LTS from https://nodejs.org/
    pause
    exit /b 1
)
for /f "tokens=1" %%a in ('npm --version 2^>^&1') do echo   [OK] npm v%%a

:: ============================================================
:: 4. AI CLI
:: ============================================================
echo [4/6] AI CLI Setup

set CB_INSTALLED=0
set CL_INSTALLED=0
where codebuddy >nul 2>&1 && set CB_INSTALLED=1
where claude >nul 2>&1 && set CL_INSTALLED=1

echo.
echo   Available AI CLI options:
if %CB_INSTALLED% equ 1 (
    echo   [1] CodeBuddy CLI  ^(already installed^)
) else (
    echo   [1] CodeBuddy CLI  ^(will install via npm^)
)
if %CL_INSTALLED% equ 1 (
    echo   [2] Claude Code     ^(already installed^)
) else (
    echo   [2] Claude Code     ^(will install via npm^)
)
echo.

:select_cli
set /p CLI_CHOICE="   Choose CLI (1 or 2): "

set SELECTED_CLI=
set PKG_NAME=

if "%CLI_CHOICE%"=="1" (
    set SELECTED_CLI=codebuddy
    set PKG_NAME=@tencent/codebuddy-cli
    if %CB_INSTALLED% equ 1 (set NEED_INSTALL=0) else (set NEED_INSTALL=1)
)
if "%CLI_CHOICE%"=="2" (
    set SELECTED_CLI=claude
    set PKG_NAME=@anthropic-ai/claude-code
    if %CL_INSTALLED% equ 1 (set NEED_INSTALL=0) else (set NEED_INSTALL=1)
)

if "%SELECTED_CLI%"=="" (
    echo   Invalid, enter 1 or 2.
    goto select_cli
)

if "%NEED_INSTALL%"=="1" (
    echo.
    echo   Installing %PKG_NAME% ...
    call npm install -g %PKG_NAME%
    if !errorlevel! neq 0 (
        echo   [WARN] Install failed. Try manually: npm install -g %PKG_NAME%
    ) else (
        echo   [OK] %SELECTED_CLI% installed.
    )
) else (
    echo   [OK] %SELECTED_CLI% is already installed.
)

echo {"cli": "%SELECTED_CLI%"} > "%cd%\cli_config.json"
echo   Config saved: .agent_os\cli_config.json

:: ============================================================
:: 5. Virtual Env
:: ============================================================
echo.
echo [5/6] Setting up virtual environment...
set VENV_DIR=%cd%\..\.venv

if not exist "%VENV_DIR%\Scripts\python.exe" (
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo   Virtual environment created.
) else (
    echo   Virtual environment already exists, skipping.
)

:: ============================================================
:: 6. Python Dependencies
:: ============================================================
echo [6/6] Installing Python dependencies...

call "%VENV_DIR%\Scripts\activate.bat"

if exist "%cd%\requirements.txt" (
    pip install -r "%cd%\requirements.txt" --quiet
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo   Dependencies installed.
) else (
    echo   [WARN] requirements.txt not found, skipping.
)

:: ============================================================
echo.
echo ========================================
echo   Setup complete!
echo.
echo   Start Agent OS:
echo     .agent_os\start.bat
echo ========================================
echo.

endlocal
pause
