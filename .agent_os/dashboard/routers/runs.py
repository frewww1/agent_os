"""Agent API 路由 — /api/agent/*"""
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..models import RunRequest, ContinueRequest, QualityPolicyRequest, PlanDecisionRequest, LabelRequest, ReportRequest, SendMsgRequest
from .deps import get_agent_os

router = APIRouter(prefix="/api", tags=["agent"])
logger = logging.getLogger("agent_os")


@router.post("/agent")
async def start_agent(req: RunRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    try:
        agent_id = agent_os.start_agent(prompt=req.prompt, agent_name=req.agent_name,
                              model=req.model, workspace_name=req.workspace_name,
                              system_prompt=req.system_prompt,
                              task_type=req.task_type,
                              interactive=req.interactive,
                              goal=req.goal, supervisor=req.supervisor)
        if req.max_goal_retries is not None and agent_id:
            agent_os.set_goal(agent_id, req.goal or "", max_retries=req.max_goal_retries)
        return JSONResponse({"agent_id": agent_id})
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"CLI command '{agent_os.cli_command}' not found."}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/agent/{agent_id}/stream")
async def stream_agent(agent_id: str):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)

    async def event_generator():
        async for line in agent_os.stream_output(agent_id):
            yield f"data: {line}\n\n"
        agent = agent_os.get_agent(agent_id)
        status = agent.status.value if agent else "unknown"
        yield f"event: done\ndata: {status}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/agents")
async def list_agents():
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"agents": []})
    return JSONResponse({"agents": agent_os.list_agents()})


@router.get("/agent/{agent_id}")
async def get_agent(agent_id: str):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    try:
        plan_content = None
        plan_file = None
        if agent.status.value == "plan_pending":
            from ...src.core.agents import find_latest_plan_file
            plan_file = find_latest_plan_file()
            if plan_file:
                try:
                    with open(plan_file, "r", encoding="utf-8", errors="replace") as pf:
                        plan_content = pf.read()[:10000]
                except Exception:
                    pass
        return JSONResponse({
            "agent_id": agent.agent_id, "prompt": agent.prompt,
            "status": agent.status.value, "session_id": agent.session_id,
            "parent_id": agent.parent_id,
            "children_ids": agent.children_ids,
            "started_at": agent.started_at.isoformat(),
            "completed_at": agent.completed_at.isoformat() if agent.completed_at else None,
            "exit_code": agent.exit_code,
            "events": list(agent.output_events),
            "turns": len(agent.turn_markers),
            "messages": list(agent.messages),
            "reported_result": agent.reported_result,
            "interactive": agent.interactive,
            "task_type": agent.task_type,
            "model": agent.model,
            "user_terminated": agent.user_terminated,
            "label": agent.label,
            "workspace_files": _list_workspace_files(agent_id),
            "system_prompt": agent.system_prompt,
            "goal": agent.goal,
            "goal_retries": agent.goal_retries,
            "max_goal_retries": agent.max_goal_retries or agent_os.MAX_GOAL_RETRIES,
            "supervisor": agent.supervisor,
            "plan_content": plan_content,
            "plan_file": plan_file,
        })
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()}, status_code=500)


def _list_workspace_files(agent_id: str) -> list[str]:
    from pathlib import Path
    from .workspace import _get_workspace_dir
    workspace_dir = _get_workspace_dir(agent_id)
    if not workspace_dir.exists():
        return []
    files = []
    for p in sorted(workspace_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace_dir)
        files.append(str(rel).replace("\\", "/"))
    return files


@router.post("/agent/{agent_id}/continue")
async def continue_agent(agent_id: str, req: ContinueRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    if not agent.session_id:
        return JSONResponse({"error": "no session_id, cannot continue"}, status_code=400)
    try:
        success = agent_os.continue_agent(agent_id, prompt=req.prompt, model=req.model, goal=req.goal)
        if success:
            return JSONResponse({"ok": True, "agent_id": agent_id})
        else:
            return JSONResponse({"error": "cannot continue"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/agent/{agent_id}/plan/approve")
async def approve_plan(agent_id: str, req: PlanDecisionRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = agent_os.approve_plan(agent_id, feedback=req.feedback, model=req.model)
    if success:
        return JSONResponse({"ok": True, "agent_id": agent_id})
    return JSONResponse({"error": "cannot approve"}, status_code=400)


@router.post("/agent/{agent_id}/plan/reject")
async def reject_plan(agent_id: str, req: PlanDecisionRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = agent_os.reject_plan(agent_id, feedback=req.feedback, model=req.model)
    if success:
        return JSONResponse({"ok": True, "agent_id": agent_id})
    return JSONResponse({"error": "cannot reject"}, status_code=400)


@router.post("/agent/{agent_id}/set-goal")
async def set_goal(agent_id: str, req: Request):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    goal = body.get("goal", "")
    max_retries = body.get("max_retries")
    if max_retries is not None:
        try:
            max_retries = int(max_retries)
        except (TypeError, ValueError):
            return JSONResponse({"error": "max_retries must be int"}, status_code=400)
    ok = agent_os.set_goal(agent_id, goal, max_retries=max_retries)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "agent not found"}, status_code=404)


@router.post("/agent/{agent_id}/quality-policy")
async def set_quality_policy(agent_id: str, req: QualityPolicyRequest):
    """统一更新 Agent 的完成目标、评估预算和监督审查标准。"""
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    max_retries = max(1, min(int(req.max_retries), 99))
    agent.set_goal((req.goal or "").strip(), max_retries=max_retries)
    agent.supervisor = (req.supervisor or "").strip() or None
    agent._dirty = True
    agent.add_event(
        "system",
        text=("[Agent OS] Quality protocol updated: "
              f"goal={'on' if agent.goal else 'off'}, "
              f"supervisor={'on' if agent.supervisor else 'off'}, "
              f"retry budget={max_retries}"),
    )
    return JSONResponse({
        "ok": True,
        "goal": agent.goal,
        "supervisor": agent.supervisor,
        "max_goal_retries": max_retries,
    })


@router.post("/agent/{agent_id}/skip-goal")
async def skip_goal(agent_id: str):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    ok = agent_os.skip_goal(agent_id)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "agent not found"}, status_code=404)


@router.post("/agent/{agent_id}/set-max-goal-retries")
async def set_max_goal_retries(agent_id: str, req: Request):
    """动态调整单个 agent 的 goal 最大重试次数。"""
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    try:
        body = await req.json()
        max_retries = int(body.get("max_retries", 5))
    except Exception:
        return JSONResponse({"error": "invalid JSON or max_retries"}, status_code=400)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    agent.max_goal_retries = max_retries
    return JSONResponse({"ok": True, "max_retries": max_retries})


@router.post("/agent/{agent_id}/stop")
async def stop_agent(agent_id: str):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = agent_os.stop_agent(agent_id)
    if success:
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "cannot stop"}, status_code=400)


@router.delete("/agent/{agent_id}")
async def delete_agent(agent_id: str):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    deleted = agent_os.delete_agent(agent_id, recursive=True)
    return JSONResponse({"deleted": deleted})


@router.post("/agent/{agent_id}/rewind")
async def rewind_agent(agent_id: str, req: dict):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    target_ts = req.get("ts", "")
    if not target_ts:
        return JSONResponse({"error": "ts is required"}, status_code=400)
    result = agent_os.rewind_to(agent_id, target_ts)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "rewind failed")}, status_code=400)
    return JSONResponse(result)


@router.post("/agents/clear")
async def clear_agents():
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    count = agent_os.clear_completed()
    return JSONResponse({"cleared": count})


@router.post("/agent/{agent_id}/clear")
async def clear_agent_context(agent_id: str):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    result = agent_os.clear_context(agent_id)
    if result.get("ok"):
        return JSONResponse(result)
    return JSONResponse(result, status_code=400)


@router.post("/agent/{agent_id}/complete")
async def complete_interactive(agent_id: str):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = agent_os.complete_interactive(agent_id)
    if success:
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "cannot complete"}, status_code=400)


@router.post("/agent/{agent_id}/send")
async def send_message(agent_id: str, req: SendMsgRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    if not agent_os.get_agent(agent_id):
        return JSONResponse({"error": "agent not found"}, status_code=404)
    logger.info(f"[{agent_id[:8]}] send_message received: {req.msg[:100]}")
    agent_os.handle_send(agent_id, req.msg)
    return JSONResponse({"ok": True})


@router.post("/agent/{agent_id}/report")
async def report_result(agent_id: str, req: ReportRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    agent.reported_result = req.result
    agent_os.report_complete(agent_id, req.result)
    return JSONResponse({"ok": True})


@router.post("/agent/{agent_id}/label")
async def set_label(agent_id: str, req: LabelRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not ready"}, status_code=503)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    agent.label = req.label.strip() or None
    agent._dirty = True
    return JSONResponse({"ok": True})
