@echo off
chcp 65001 >nul

cd /d "%~dp0"

if not exist "..\.venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Read CLI choice from config
set CLI_NAME=codebuddy
if exist "%cd%\cli_config.json" (
    for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-Content '%cd%\cli_config.json' -Encoding UTF8 | ConvertFrom-Json).cli" 2^>nul') do set CLI_NAME=%%i
)

:: Resolve to full path (try PATH, then common node global dirs)
set CLI=
for /f "delims=" %%i in ('where %CLI_NAME% 2^>nul') do set "CLI=%%i"
if "%CLI%"=="" if defined NVM_SYMLINK if exist "%NVM_SYMLINK%\%CLI_NAME%.cmd" set "CLI=%NVM_SYMLINK%\%CLI_NAME%.cmd"
if "%CLI%"=="" if exist "%ProgramFiles%\nodejs\%CLI_NAME%.cmd" set "CLI=%ProgramFiles%\nodejs\%CLI_NAME%.cmd"
if "%CLI%"=="" set "CLI=%CLI_NAME%"

echo Starting Agent OS with CLI: %CLI%
echo.

:: --- Phase 0: Kill existing agent_os process ---
echo Stopping any existing agent_os...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8420.*LISTENING"') do taskkill /pid %%a /f /t 2>nul
powershell -NoProfile -Command "Get-Process python -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -like '*agent_os*'} | Stop-Process -Force" 2>nul
timeout /t 3 /nobreak >nul
echo.

:: --- Phase 1: Parse available models from CLI --help ---
mkdir "%cd%\state" 2>nul

:: --- Phase 1: Parse models from CLI --help (works for both codebuddy v2.52+ and claude) ---
echo Detecting models from %CLI_NAME% --help...
call "%CLI%" --help > "%cd%\state\help_output.txt" 2>&1
:: Write to per-CLI file: models_codebuddy.json / models_claude.json etc.
set MODELS_FILE=%cd%\state\models_%CLI_NAME%.json
python -c "import json,re; t=open(r'%cd%\state\help_output.txt',encoding='utf-8',errors='replace').read(); m=re.search(r'Currently\s+supported:\s*\(([^)]+)\)',t,re.DOTALL); BAD={'echo'}; models=[s.strip() for s in m.group(1).split(',') if s.strip() and s.strip() not in BAD] if m else []; print(f'  Found {len(models)} model(s): {models}'); [models] and json.dump(models,open(r'%MODELS_FILE%','w',encoding='utf-8'),ensure_ascii=False)" 2>nul
if %errorlevel% neq 0 echo   [WARN] Model detection skipped (will use built-in list)
echo.

:: --- Phase 2: Start server ---
call ..\.venv\Scripts\activate.bat
python main.py --cli "%CLI%"
pause
