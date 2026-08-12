"""DAG API 路由 — /api/dag/*, /api/agent/{id}/dag*"""
import json
import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..models import DagStartRequest
from .deps import get_agent_os, get_project_root

router = APIRouter(prefix="/api", tags=["dag"])

DAG_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "dag_templates"


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

    # workspace 必须跟随 project_root（与 build_agent_env 的 $AGENT_OS_WORKSPACE 一致），
    # 否则切换工作根后 dag.json 落在安装目录，调度 agent 在自己的 workspace 里读不到。
    root = get_project_root()
    if root:
        ws_dir = root / "workspaces" / req.workspace_name
    else:
        ws_dir = Path(__file__).parent.parent.parent / "workspaces" / req.workspace_name

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

    steps_desc = "\n".join(
        f"  {i+1}. {s.get('name', s['id'])} ({s['id']}){' ← ' + ', '.join(s.get('depends_on', [])) if s.get('depends_on') else ''}"
        for i, s in enumerate(steps)
    )
    _aos_dir = str(Path(__file__).parent.parent.parent).replace("\\", "/")
    _dag_py = f"python {_aos_dir}/dag.py"
    _spawn_py = f"python {_aos_dir}/spawn.py"
    _report_py = f"python {_aos_dir}/report.py"
    system_prompt = (
        f"你是 DAG 调度 agent。按模板顺序执行流水线：\n\n{steps_desc}\n\n"
        f"dag.json 位置：{ws_dir}/dag.json（绝对路径）。\n\n"
        f"执行方式（全部使用绝对路径，脚本会自动定位 dag.json）：\n"
        f"1. `{_dag_py} --ready` → 取就绪节点（JSON 数组，含 "
        f"id/prompt/type/goal/supervisor）\n"
        f"2. 对每个就绪节点，用 spawn.py 创建子 agent：\n"
        f"   `{_spawn_py} --tasks '[{{\"prompt\":\"...\","
        f"\"type\":\"<interactive|generative>\",\"step_id\":\"<节点id>\","
        f"\"goal\":\"...\",\"supervisor\":\"...\"}}]'`\n"
        f"   - **必须保留 --ready 返回的所有字段**（prompt/type/goal/supervisor/step_id）\n"
        f"   - interactive: 子 agent 等用户在 Dashboard 点 Done\n"
        f"   - generative: 子 agent 自行调 report.py 结束\n"
        f"3. 派发完所有就绪节点后结束对话，等 OS 自动 resume\n"
        f"4. resume 后 `{_dag_py} --mark-done <id>`，回到第 1 步\n"
        f"5. --ready 返回空时 `{_report_py} --result \"全部完成\"`\n\n"
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
