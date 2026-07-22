"""Run API 路由 — /api/run/*"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..models import RunRequest, ContinueRequest, PlanDecisionRequest, LabelRequest, ReportRequest, SendMsgRequest

router = APIRouter(prefix="/api", tags=["run"])


def get_pm():
    """获取全局 ProcessManager。由 app.py 在初始化时注入。"""
    from ..app import pm
    return pm


@router.post("/run")
async def start_run(req: RunRequest):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    try:
        run_id = pm.start_run(prompt=req.prompt, agent_name=req.agent_name,
                              model=req.model, workspace_name=req.workspace_name,
                              system_prompt=req.system_prompt,
                              task_type=req.task_type,
                              interactive=req.interactive,
                              goal=req.goal, supervisor=req.supervisor)
        if req.max_goal_retries is not None and run_id:
            pm.set_goal(run_id, req.goal or "", max_retries=req.max_goal_retries)
        return JSONResponse({"run_id": run_id})
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"CLI command '{pm.cli_command}' not found."}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/run/{run_id}/stream")
async def stream_run(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)

    async def event_generator():
        async for line in pm.stream_output(run_id):
            yield f"data: {line}\n\n"
        run_info = pm.get_run(run_id)
        status = run_info.status.value if run_info else "unknown"
        yield f"event: done\ndata: {status}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/runs")
async def list_runs():
    pm = get_pm()
    if not pm:
        return JSONResponse({"runs": []})
    return JSONResponse({"runs": pm.list_runs()})


@router.get("/run/{run_id}")
async def get_run(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    try:
        plan_content = None
        plan_file = None
        if run_info.status.value == "plan_pending":
            plan_file = pm._find_latest_plan_file()
            if plan_file:
                try:
                    with open(plan_file, "r", encoding="utf-8", errors="replace") as pf:
                        plan_content = pf.read()[:10000]
                except Exception:
                    pass
        return JSONResponse({
            "run_id": run_info.run_id, "prompt": run_info.prompt,
            "status": run_info.status.value, "session_id": run_info.session_id,
            "parent_run_id": run_info.parent_run_id,
            "children_run_ids": run_info.children_run_ids,
            "started_at": run_info.started_at.isoformat(),
            "completed_at": run_info.completed_at.isoformat() if run_info.completed_at else None,
            "exit_code": run_info.exit_code,
            "events": list(run_info.output_events),
            "turns": len(run_info.turn_markers),
            "messages": list(run_info.messages),
            "reported_result": run_info.reported_result,
            "interactive": run_info.interactive,
            "task_type": run_info.task_type,
            "model": run_info.model,
            "user_terminated": run_info.user_terminated,
            "label": run_info.label,
            "workspace_files": _list_workspace_files(run_id),
            "system_prompt": run_info.system_prompt,
            "goal": run_info.goal,
            "goal_retries": run_info.goal_retries,
            "max_goal_retries": getattr(run_info, '_max_goal_retries', None) or pm.MAX_GOAL_RETRIES,
            "plan_content": plan_content,
            "plan_file": plan_file,
        })
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()}, status_code=500)


def _list_workspace_files(run_id: str) -> list[str]:
    from pathlib import Path
    from .workspace import _get_workspace_dir
    workspace_dir = _get_workspace_dir(run_id)
    if not workspace_dir.exists():
        return []
    files = []
    for p in sorted(workspace_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace_dir)
        files.append(str(rel).replace("\\", "/"))
    return files


@router.post("/run/{run_id}/continue")
async def continue_run(run_id: str, req: ContinueRequest):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    if not run_info.session_id:
        return JSONResponse({"error": "no session_id, cannot continue"}, status_code=400)
    try:
        success = pm.continue_run(run_id, prompt=req.prompt, model=req.model, goal=req.goal)
        if success:
            return JSONResponse({"ok": True, "run_id": run_id})
        else:
            return JSONResponse({"error": "cannot continue"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/run/{run_id}/plan/approve")
async def approve_plan(run_id: str, req: PlanDecisionRequest):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = pm.approve_plan(run_id, feedback=req.feedback, model=req.model)
    if success:
        return JSONResponse({"ok": True, "run_id": run_id})
    return JSONResponse({"error": "cannot approve"}, status_code=400)


@router.post("/run/{run_id}/plan/reject")
async def reject_plan(run_id: str, req: PlanDecisionRequest):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = pm.reject_plan(run_id, feedback=req.feedback, model=req.model)
    if success:
        return JSONResponse({"ok": True, "run_id": run_id})
    return JSONResponse({"error": "cannot reject"}, status_code=400)


@router.post("/run/{run_id}/set-goal")
async def set_goal(run_id: str, req: Request):
    pm = get_pm()
    if not pm:
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
    ok = pm.set_goal(run_id, goal, max_retries=max_retries)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "run not found"}, status_code=404)


@router.post("/run/{run_id}/skip-goal")
async def skip_goal(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    ok = pm.skip_goal(run_id)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "run not found"}, status_code=404)


@router.post("/run/{run_id}/set-max-goal-retries")
async def set_max_goal_retries(run_id: str, req: Request):
    """动态调整单个 run 的 goal 最大重试次数。"""
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    try:
        body = await req.json()
        max_retries = int(body.get("max_retries", 5))
    except Exception:
        return JSONResponse({"error": "invalid JSON or max_retries"}, status_code=400)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    run_info._max_goal_retries = max_retries
    return JSONResponse({"ok": True, "max_retries": max_retries})


@router.post("/run/{run_id}/stop")
async def stop_run(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = pm.stop_run(run_id)
    if success:
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "cannot stop"}, status_code=400)


@router.delete("/run/{run_id}")
async def delete_run(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    deleted = pm.delete_run(run_id, recursive=True)
    return JSONResponse({"deleted": deleted})


@router.post("/run/{run_id}/rewind")
async def rewind_run(run_id: str, req: dict):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    try:
        target_seq = int(req.get("seq"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "seq must be int"}, status_code=400)
    result = pm.rewind_to(run_id, target_seq)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "rewind failed")}, status_code=400)
    return JSONResponse(result)


@router.post("/runs/clear")
async def clear_runs():
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    count = pm.clear_completed()
    return JSONResponse({"cleared": count})


@router.post("/run/{run_id}/clear")
async def clear_run_context(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    result = pm.clear_context(run_id)
    if result.get("ok"):
        return JSONResponse(result)
    return JSONResponse(result, status_code=400)


@router.post("/run/{run_id}/complete")
async def complete_interactive(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = pm.complete_interactive(run_id)
    if success:
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "cannot complete"}, status_code=400)


@router.post("/run/{run_id}/send")
async def send_message(run_id: str, req: SendMsgRequest):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    from datetime import datetime
    run_info.messages.append({"time": datetime.now().isoformat(), "msg": req.msg})
    run_info.add_event("send", text=req.msg)

    # 如果 agent 正在等待 supervisor 审查，收到消息后 resume
    waiting_sup_id = getattr(run_info, '_waiting_supervisor', None)
    if waiting_sup_id:
        msg_upper = req.msg.strip().upper()
        if msg_upper.startswith("PASS"):
            # 监督通过：清除状态，触发 _on_run_completed 做 spawn resolution
            object.__setattr__(run_info, '_waiting_supervisor', None)
            run_info.supervisor = None
            run_info.add_text_line("[Agent OS] Supervisor: PASS — task complete", kind="system")
            if pm:
                pm._on_run_completed(run_info)
                pm._mark_dirty()
        elif msg_upper.startswith("CORRECTION"):
            correction = req.msg.strip()
            # 仅清除等待状态，保留 supervisor 字段以便下轮 resume 同一 supervisor
            object.__setattr__(run_info, '_waiting_supervisor', None)
            run_info.add_text_line(f"[Agent OS] Supervisor correction: {correction[:200]}", kind="system")
            if pm:
                pm.continue_run(run_id, correction, source="os")
                pm._mark_dirty()

    return JSONResponse({"ok": True})


@router.post("/run/{run_id}/report")
async def report_result(run_id: str, req: ReportRequest):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    run_info.reported_result = req.result
    pm.report_complete(run_id, req.result)
    return JSONResponse({"ok": True})


@router.post("/run/{run_id}/label")
async def set_label(run_id: str, req: LabelRequest):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not ready"}, status_code=503)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    run_info.label = req.label.strip() or None
    pm._mark_dirty()
    return JSONResponse({"ok": True})
