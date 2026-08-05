"""Agent OS Dashboard — Web terminal with spawn/resume and tree visualization."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..src.core.agent_os import AgentOS

app = FastAPI(title="Agent OS")

DASHBOARD_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")

# AgentOS 实例（由 main.py 注入）
agent_os: AgentOS | None = None


def set_agent_os(aos: AgentOS):
    global agent_os
    agent_os = aos


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/api/info")
async def info():
    """运行信息：当前服务的工作根目录（project_root）等，供前端展示。"""
    if agent_os is None:
        return {"ok": False, "error": "not initialized"}
    return {
        "ok": True,
        "project_root": agent_os.project_root,
        "port": agent_os.port,
        "cli": agent_os.cli_command,
        "backend": type(agent_os._backend).__name__,
        "default_model": agent_os.default_model,
        "agents": len(agent_os.agents),
    }


# 注册子路由
from .routers import runs, workspace, dag, spawn, export, root
app.include_router(runs.router)
app.include_router(workspace.router)
app.include_router(dag.router)
app.include_router(spawn.router)
app.include_router(export.router)
app.include_router(root.router)
