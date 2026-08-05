@echo off
rem ============================================================
rem  Agent OS 全局启动器（Windows）
rem  用法：把本文件所在目录加入 PATH，然后在任意项目目录执行：
rem      agent_os [--port 8420] [--no-browser] ...
rem  服务以"当前 CLI 运行目录"为工作根目录（project_root），
rem  agent 的 workspace / state 都会落在当前目录下。
rem ============================================================
setlocal
set "ROOT=%cd%"

if exist "%~dp0..\.venv\Scripts\python.exe" (
    set "PY=%~dp0..\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" "%~dp0main.py" --root "%ROOT%" %*
endlocal
