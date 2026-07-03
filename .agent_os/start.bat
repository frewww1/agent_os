@echo off
cd /d "%~dp0\.."
python .agent_os\main.py --cli codebuddy --port 8420 %*