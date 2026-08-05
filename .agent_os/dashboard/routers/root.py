"""Root switch API — 前端切换当前工作根目录（project_root）。

只改路径，不重建实例：AgentOS.switch_root() 内部完成落盘、换路径、
重载新目录历史。前提是没有运行中的 agent，否则拒绝切换。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .deps import get_agent_os

router = APIRouter(prefix="/api", tags=["root"])


@router.get("/root/candidates")
async def root_candidates():
    aos = get_agent_os()
    if not aos:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    return {"ok": True, "current": aos.project_root, "candidates": aos.root_candidates()}


@router.post("/root/switch")
async def switch_root(req: dict):
    aos = get_agent_os()
    if not aos:
        return JSONResponse({"error": "not initialized"}, status_code=500)

    path = (req or {}).get("path")
    if not path or not isinstance(path, str):
        return JSONResponse({"error": "path required"}, status_code=400)

    result = aos.switch_root(path)
    if not result.get("ok"):
        if result.get("busy_agents"):
            return JSONResponse(result, status_code=409)
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)
