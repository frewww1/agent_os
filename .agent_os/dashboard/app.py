"""Agent OS Dashboard — Web terminal with spawn/resume and tree visualization."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..src.core.process_manager import ProcessManager

app = FastAPI(title="Agent OS")

DASHBOARD_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")

# Process manager (initialized in main.py)
pm: ProcessManager | None = None


def set_process_manager(process_manager: ProcessManager):
    global pm
    pm = process_manager


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


# 注册子路由
from .routers import runs, workspace, dag, spawn, export
app.include_router(runs.router)
app.include_router(workspace.router)
app.include_router(dag.router)
app.include_router(spawn.router)
app.include_router(export.router)
