"""Spawn + Agent 通信 API — /api/spawn, /api/tree"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..models import SpawnRequest

router = APIRouter(prefix="/api", tags=["spawn"])


def get_agent_os():
    from ..app import agent_os
    return agent_os


@router.post("/spawn")
async def spawn_children(req: SpawnRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    tasks = [{"prompt": t.prompt, "agent_name": t.agent_name, "type": t.type,
              "model": t.model, "step_id": t.step_id,
              "goal": t.goal, "supervisor": t.supervisor} for t in req.tasks]
    result = agent_os.spawn_children(
        parent_run_id=req.parent_run_id, parent_session_id=req.parent_session_id,
        tasks=tasks, wait_strategy=req.wait_strategy,
    )
    return JSONResponse(result)


@router.get("/tree")
async def get_tree(workspace_id: str = ""):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"tree": []})
    tree = agent_os.get_tree()
    if workspace_id:
        tree = _filter_tree_by_workspace(tree, workspace_id)
    return JSONResponse({"tree": tree})


def _filter_tree_by_workspace(tree: list, workspace_id: str) -> list:
    def _ws_tail(wp):
        if not wp:
            return ""
        norm = wp.replace("\\", "/").rstrip("/")
        return norm.rsplit("/", 1)[-1] if norm else ""

    def _filter_node(node: dict):
        if _ws_tail(node.get("workspace_path", "")) == workspace_id:
            return node
        filtered_children = []
        for child in node.get("children", []):
            fc = _filter_node(child)
            if fc:
                filtered_children.append(fc)
        if filtered_children:
            node["children"] = filtered_children
            return node
        return None

    result = []
    for node in tree:
        fn = _filter_node(node)
        if fn:
            result.append(fn)
    return result
