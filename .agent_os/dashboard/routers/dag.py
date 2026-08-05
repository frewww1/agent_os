"""DAG API 路由 — /api/dag/*, /api/agent/{id}/dag*"""
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..models import DagStartRequest
from .deps import get_agent_os, get_project_root, safe_run as _safe_run

router = APIRouter(prefix="/api", tags=["dag"])

DAG_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "dag_templates"


def _resolve_workspace_dir(workspace_id: str) -> Path | None:
    workspaces_dir = Path(__file__).parent.parent.parent / "workspaces"
    ws_dir = workspaces_dir / workspace_id
    if not ws_dir.is_dir():
        for d in workspaces_dir.iterdir():
            if d.is_dir() and d.name.startswith(workspace_id):
                ws_dir = d
                break
    return ws_dir if ws_dir.is_dir() else None


@router.get("/dag/templates")
async def list_dag_templates():
    templates = []
    if DAG_TEMPLATES_DIR.is_dir():
        for f in sorted(DAG_TEMPLATES_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                templates.append({
                    "id": f.stem,
                    "name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "steps": data.get("steps", []),
                })
            except Exception:
                pass
    return JSONResponse({"templates": templates})


@router.post("/dag/start")
async def start_dag(req: DagStartRequest):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    # from agent_os.src.persistence.git_recorder import Recorder as _Recorder  # TODO: git 功能暂时禁用

    aos_dir = Path(__file__).parent.parent.parent
    ws_dir = aos_dir / "workspaces" / req.workspace_name

    if req.resume:
        if not ws_dir.is_dir():
            return JSONResponse({"error": f"workspace '{req.workspace_name}' not found"}, status_code=404)
        dag_file = ws_dir / "dag.json"
        if not dag_file.is_file():
            return JSONResponse({"error": "dag.json not found in workspace"}, status_code=400)
        try:
            dag = json.loads(dag_file.read_text(encoding="utf-8"))
        except Exception as e:
            return JSONResponse({"error": f"load dag.json failed: {e}"}, status_code=500)
        steps = dag.get("steps", [])
        if not steps:
            return JSONResponse({"error": "dag.json has no steps"}, status_code=400)
    else:
        template_file = DAG_TEMPLATES_DIR / f"{req.template_id}.json"
        if not template_file.is_file():
            return JSONResponse({"error": f"template '{req.template_id}' not found"}, status_code=404)
        try:
            template = json.loads(template_file.read_text(encoding="utf-8"))
        except Exception as e:
            return JSONResponse({"error": f"load template failed: {e}"}, status_code=500)
        steps = template.get("steps", [])
        if not steps:
            return JSONResponse({"error": "template has no steps"}, status_code=400)
        os.makedirs(ws_dir, exist_ok=True)
        dag = {"steps": []}
        for s in steps:
            step = dict(s)
            step["status"] = "pending"
            dag["steps"].append(step)
        dag_file = ws_dir / "dag.json"
        dag_file.write_text(json.dumps(dag, indent=2, ensure_ascii=False), encoding="utf-8")

    # git 功能暂时禁用
    # try:
    #     project_root = str(_get_project_root()) if _get_project_root() else None
    #     rec = _Recorder(project_root=project_root)
    #     rec.ensure_task_branch(str(ws_dir), agent_name=req.workspace_name)
    #     rec.baseline_commit(str(ws_dir), agent_name=req.workspace_name)
    #     if not req.resume:
    #         rec.step_done(run_id="init", step_id="__init__",
    #                       workspace_path=str(ws_dir), message="DAG initialized")
    #     else:
    #         from agent_os.src.core import dag_planner as dp
    #         dag = dp.load_dag(str(ws_dir))
    #         steps_list = dag.get("steps", [])
    #         reset_count = 0
    #         for s in steps_list:
    #             if s.get("status") == "running":
    #                 s["status"] = "pending"
    #                 reset_count += 1
    #         if reset_count > 0:
    #             dp.save_dag(str(ws_dir), dag)
    # except Exception:
    #     pass

    steps_desc = "\n".join(
        f"  {i+1}. {s.get('name', s['id'])} ({s['id']}){' ← ' + ', '.join(s.get('depends_on', [])) if s.get('depends_on') else ''}"
        for i, s in enumerate(steps)
    )
    system_prompt = (
        f"你是 DAG 调度 agent。按模板顺序执行流水线：\n\n{steps_desc}\n\n"
        f"dag.json 位置：$AGENT_OS_WORKSPACE/dag.json（环境变量已设好，dag.py --ready 自动读取）。\n\n"
        f"执行方式：\n"
        f"1. `python .agent_os/dag.py --ready` → 取就绪节点（JSON 数组，含 "
        f"id/prompt/type/goal/supervisor）\n"
        f"2. 对每个就绪节点，用 spawn.py 创建子 agent：\n"
        f"   `python .agent_os/spawn.py --tasks '[{{\"prompt\":\"...\","
        f"\"type\":\"<interactive|generative>\",\"step_id\":\"<节点id>\","
        f"\"goal\":\"...\",\"supervisor\":\"...\"}}]'`\n"
        f"   - **必须保留 --ready 返回的所有字段**（prompt/type/goal/supervisor/step_id）\n"
        f"   - interactive: 子 agent 等用户在 Dashboard 点 Done\n"
        f"   - generative: 子 agent 自行调 report.py 结束\n"
        f"3. 派发完所有就绪节点后结束对话，等 OS 自动 resume\n"
        f"4. resume 后 `python .agent_os/dag.py --mark-done <id>`，回到第 1 步\n"
        f"5. --ready 返回空时 `python .agent_os/report.py --result \"全部完成\"`\n\n"
        f"Supervisor 机制：\n"
        f"- 带 supervisor 的 step，子 agent 完成后 OS 自动启动审查 agent\n"
        f"- 审查 agent 严格检查产出是否满足所有标准，全部通过才回复 PASS\n"
        f"- 有问题回复 CORRECTION: <具体反馈>，OS 自动让子 agent 修正重试\n"
        f"- 修正重试是自动的，调度 agent 无需干预，resume 时 step 已完成"
    )
    prompt = f"请继续执行 DAG，任务名: {req.workspace_name}" if req.resume \
        else f"请执行 DAG 模板: {req.template_id}，任务名: {req.workspace_name}"
    agent_id = agent_os.start_agent(prompt=prompt, agent_name=req.workspace_name,
                          model=req.model, workspace_name=req.workspace_name,
                          system_prompt=system_prompt)
    return JSONResponse({"agent_id": agent_id, "template": req.template_id, "resume": req.resume})


@router.get("/agent/{agent_id}/dag")
async def get_dag(agent_id: str, workspace_id: str = ""):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    if workspace_id:
        result = agent_os.dag_status_by_workspace(workspace_id)
    else:
        result = agent_os.dag_status(agent_id)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "no dag")}, status_code=400)
    return JSONResponse(result)


@router.post("/agent/{agent_id}/dag/checkout")
async def dag_checkout(agent_id: str, req: dict):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    step_id = req.get("step_id")
    if not step_id:
        return JSONResponse({"error": "step_id required"}, status_code=400)
    result = agent_os.dag_checkout(agent_id, step_id,
                             rerun_downstream=bool(req.get("rerun_downstream")))
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "checkout failed")}, status_code=400)
    return JSONResponse(result)


@router.get("/dag/{workspace_id}/steps")
async def dag_steps(workspace_id: str):
    ws_dir = _resolve_workspace_dir(workspace_id)
    if not ws_dir:
        return JSONResponse({"error": "workspace not found"}, status_code=404)
    try:
        r = _safe_run(
            ["git", "log", "-F", "--grep", "[step:", "--format=%H%x09%ct%x09%s"],
            cwd=str(ws_dir), capture_output=True, text=True, timeout=10)
        steps = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            commit_hash, ts, msg = parts
            m = re.match(r"\[step:([^\]]+)\]\s*(.*)", msg)
            if not m:
                continue
            steps.append({
                "step_id": m.group(1), "commit": commit_hash[:8],
                "ts": int(ts) if ts.isdigit() else None, "message": m.group(2),
            })
        return JSONResponse({"steps": steps})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/dag/{workspace_id}/checkout/{step_id}")
async def dag_checkout_step(workspace_id: str, step_id: str):
    return JSONResponse({"error": "git disabled"}, status_code=400)
