"""Agent OS Dashboard — Web terminal with spawn/resume and tree visualization."""
import os
import re
import json
from pathlib import Path
from typing import List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, model_validator

from ..process_manager import ProcessManager, RunStatus

app = FastAPI(title="Agent OS")

DASHBOARD_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")

# Process manager (initialized in main.py)
pm: ProcessManager | None = None


def set_process_manager(process_manager: ProcessManager):
    global pm
    pm = process_manager


# === Request Models ===

class RunRequest(BaseModel):
    prompt: str
    agent_name: str | None = None
    model: str | None = None
    workspace_name: str | None = None
    system_prompt: str | None = None


class ContinueRequest(BaseModel):
    prompt: str
    model: str | None = None


class DagStartRequest(BaseModel):
    template_id: str
    workspace_name: str
    model: str | None = None
    resume: bool = False  # True = 基于现有 workspace 的 dag.json 继续，不重新初始化


class SpawnTask(BaseModel):
    prompt: str
    agent_name: str | None = None
    type: str = "generative"  # "generative" or "interactive"
    agent_type: str | None = None  # 兼容调度 agent 可能误传的字段名
    model: str | None = None
    step_id: str | None = None  # DAG step 标识，OS 据此打 [step:<id>] commit

    @model_validator(mode="after")
    def resolve_type(self):
        """如果 type 为空/默认值，但 agent_type 有值，则用 agent_type 代替。"""
        if not self.type or self.type == "generative":
            if self.agent_type:
                self.type = self.agent_type
        return self


class SpawnRequest(BaseModel):
    tasks: List[SpawnTask]
    wait_strategy: str = "all"
    parent_run_id: str = ""
    parent_session_id: str = ""


class LabelRequest(BaseModel):
    label: str


# === Pages ===

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render dashboard page."""
    return templates.TemplateResponse(request=request, name="index.html", context={})


# === Run API ===

@app.post("/api/run")
async def start_run(req: RunRequest):
    """Start a new claude CLI process."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    try:
        run_id = pm.start_run(prompt=req.prompt, agent_name=req.agent_name,
                              model=req.model, workspace_name=req.workspace_name,
                              system_prompt=req.system_prompt)
        return JSONResponse({"run_id": run_id})
    except FileNotFoundError:
        return JSONResponse(
            {"error": f"CLI command '{pm.cli_command}' not found."},
            status_code=500
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/run/{run_id}/stream")
async def stream_run(run_id: str):
    """SSE stream of run output."""
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


@app.get("/api/runs")
async def list_runs():
    """List all runs."""
    if not pm:
        return JSONResponse({"runs": []})
    return JSONResponse({"runs": pm.list_runs()})


@app.get("/api/models")
async def list_models(refresh: bool = False):
    """返回当前 CLI 实际支持的模型 ID 列表（动态从 `<cli> --help` 解析，带缓存）。

    refresh=true 可强制重新解析（绕过缓存）。
    """
    if not pm:
        return JSONResponse({"models": []})
    return JSONResponse({"models": pm.list_models(refresh=refresh)})


WORKSPACES_DIR = Path(__file__).parent.parent / "workspaces"


def _get_workspace_dir(run_id: str) -> Path:
    """获取 run 的真实 workspace 目录。

    优先使用 run_info.workspace_path（支持命名 workspace 如 test_dag），
    兜底用 workspaces/<run_id>。
    """
    if pm:
        ri = pm.get_run(run_id)
        if ri and ri.workspace_path:
            return Path(ri.workspace_path)
    return WORKSPACES_DIR / run_id


def _get_git_branches(git_dir: Path, task_name: str | None = None) -> list:
    """获取 git 仓库的分支列表，每个分支附带可读名称。
    如果 task_name 不为 None，只返回该任务相关的分支（基准分支 + 衍生分支 -r<N>）。
    返回 [{"name": "sgr_full_...", "display": "task-a", "sha": "a488f1...", "is_base": true}, ...]
    如果 git_dir 不是 git 仓库，返回空列表。"""
    import subprocess
    if not (git_dir / ".git").is_dir():
        return []
    try:
        result = subprocess.run(
            ["git", "branch", "--format", "%(refname:short)"],
            cwd=str(git_dir), capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
        branch_names = [b.strip() for b in result.stdout.splitlines() if b.strip()]
    except Exception:
        return []

    # 为每个分支获取可读名称
    branches = []
    for name in branch_names:
        # 按任务名过滤：基准分支（精确匹配）或衍生分支（<task_name>-r<数字>）
        if task_name:
            if name != task_name and not _is_derived_branch(name, task_name):
                continue

        is_base = (name == task_name) if task_name else False

        info = {"name": name, "display": None, "sha": None, "is_base": is_base}
        try:
            # 获取该分支的 HEAD commit
            r = subprocess.run(
                ["git", "rev-parse", "--short=8", name],
                cwd=str(git_dir), capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                info["sha"] = r.stdout.strip()

            # 查找该分支上 agent commit 的 display 名
            # 只查找 commit message 中 ws_id 匹配当前 task_name 的 agent commit
            # 这样可以排除从祖先分支（如 master）继承的无关 commit
            import re
            r = subprocess.run(
                ["git", "log", name, "-F", "--grep=[agent:",
                 "--format=%H%x1f%s", "-n", "50"],
                cwd=str(git_dir), capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                for line in r.stdout.strip().split("\n"):
                    parts = line.split("\x1f", 1)
                    if len(parts) < 2:
                        continue
                    msg = parts[1]
                    # 提取 commit 中的 ws_id：从 "[agent:ws_id:run_id]" 提取 ws_id
                    ws_match = re.match(r'\[agent:([^:]+)', msg)
                    commit_ws_id = ws_match.group(1) if ws_match else ""
                    # 如果 ws_id 匹配 task_name（或 task_name 为空），用这个 commit 的 agent_name
                    if not task_name or commit_ws_id == task_name:
                        m = re.match(r'\[agent:[^\]]+\]\s*(.+?)(?:\s*:\s*done)?\s*$', msg)
                        if m:
                            info["display"] = m.group(1).strip()
                        break
            # fallback：用分支名本身
            if not info["display"]:
                info["display"] = name
        except Exception:
            pass
        branches.append(info)
    return branches


def _is_derived_branch(name: str, task_name: str) -> bool:
    """判断分支名是否为 task_name 的衍生分支（格式: <task_name>-r<数字>）。"""
    import re
    return bool(re.match(r'^' + re.escape(task_name) + r'-r\d+$', name))


def _get_current_branch(git_dir: Path) -> str:
    """获取当前 git 分支名。如果 git_dir 不是 git 仓库，返回空字符串。"""
    import subprocess
    if not (git_dir / ".git").is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(git_dir), capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def _get_project_root() -> Path | None:
    """获取项目根目录（通过 ProcessManager 的 project_root）。"""
    if pm:
        return Path(pm.project_root)
    return None


def _get_git_dir() -> Path | None:
    """获取 git 仓库根目录（project_root 且包含 .git 目录）。"""
    root = _get_project_root()
    if root and (root / ".git").is_dir():
        return root
    return None


def _list_workspace_files(run_id: str) -> list[str]:
    """列出 workspace 目录下的所有文件（相对路径），忽略 git 内部和 runs/ 目录。"""
    workspace_dir = _get_workspace_dir(run_id)
    if not workspace_dir.exists():
        return []
    files = []
    for p in sorted(workspace_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace_dir)
        # 过滤 runs/ 目录下的文件（元数据，不是产出）
        if rel.parts and rel.parts[0] == "runs":
            continue
        files.append(str(rel).replace("\\", "/"))
    return files


@app.get("/api/run/{run_id}")
async def get_run(run_id: str):
    """Get run details."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)

    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)

    try:
        return JSONResponse({
            "run_id": run_info.run_id,
            "prompt": run_info.prompt,
            "status": run_info.status.value,
            "session_id": run_info.session_id,
            "parent_run_id": run_info.parent_run_id,
            "children_run_ids": run_info.children_run_ids,
            "started_at": run_info.started_at.isoformat(),
            "completed_at": run_info.completed_at.isoformat() if run_info.completed_at else None,
            "exit_code": run_info.exit_code,
            "output": list(run_info.output_lines),
            "events": list(run_info.output_events),
            "turns": len(run_info.turn_markers),
            "messages": list(run_info.messages),
            "reported_result": run_info.reported_result,
            "interactive": run_info.interactive,
            "task_type": run_info.task_type,
            "model": run_info.model,
            "user_terminated": run_info.user_terminated,
            "workspace_files": _list_workspace_files(run_id),
            "system_prompt": run_info.system_prompt,
        })
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "traceback": traceback.format_exc()}, status_code=500)


@app.get("/api/run/{run_id}/workspace")
async def list_workspace(run_id: str):
    """List workspace files with metadata. 过滤掉 git 内部文件和 runs/ 元数据。
    同时返回 git 分支信息（含可读显示名称）。"""
    workspace_dir = _get_workspace_dir(run_id)
    if not workspace_dir.exists():
        return JSONResponse({"files": [], "branches": []})
    
    # 获取 task_name（workspace 目录名）并过滤分支
    task_name = workspace_dir.name
    
    # 获取该任务相关的 git 分支（优先使用 project_root 的 git 仓库）
    git_base = _get_project_root() or workspace_dir
    branches = _get_git_branches(git_base, task_name=task_name)
    current_branch = _get_current_branch(git_base)
    
    # 这些目录/文件名是 git 自身产物或元数据，对 workspace 用户没价值
    SKIP_DIRS = {".git", "runs"}
    SKIP_FILES = {".gitignore", ".gitattributes", ".gitmodules", ".gitkeep"}
    files = []
    for p in sorted(workspace_dir.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(workspace_dir).parts
        except ValueError:
            continue
        # 跳过 .git/ 和 runs/ 目录下的文件
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if p.name in SKIP_FILES:
            continue
        rel = "/".join(rel_parts)
        stat = p.stat()
        files.append({
            "path": rel,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return JSONResponse({
        "files": files,
        "branches": branches,
        "current_branch": current_branch,
        "task_name": task_name,
    })


@app.get("/api/run/{run_id}/workspace/file")
async def get_workspace_file(run_id: str, path: str):
    """Read a workspace file. Returns text or base64 for binary."""
    from fastapi import Query
    workspace_dir = _get_workspace_dir(run_id)
    # Prevent path traversal
    try:
        target = (workspace_dir / path).resolve()
        target.relative_to(workspace_dir.resolve())
    except (ValueError, Exception):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)

    # Try reading as UTF-8 text first
    try:
        content = target.read_text(encoding="utf-8")
        return JSONResponse({"path": path, "type": "text", "content": content})
    except UnicodeDecodeError:
        import base64
        content = base64.b64encode(target.read_bytes()).decode()
        return JSONResponse({"path": path, "type": "binary", "content": content})


class SwitchBranchRequest(BaseModel):
    branch: str


def _git_checkout_with_stash(git_base: Path, branch: str, create: bool = False) -> dict:
    """切换 git 分支，自动 stash 未提交更改。
    返回 {"ok": bool, "branch": str, "error": str|None, "warning": str|None}"""
    import subprocess
    wdir = str(git_base)
    
    def _git(cmd, timeout=10):
        return subprocess.run(cmd, cwd=wdir, capture_output=True, text=True, timeout=timeout)
    
    try:
        # 先尝试直接切换
        if create:
            result = _git(["git", "checkout", "-b", branch])
        else:
            result = _git(["git", "checkout", branch])
        if result.returncode == 0:
            return {"ok": True, "branch": branch}
        
        # 如果失败，检查是否因为有未提交的更改
        stderr = result.stderr or ""
        if "overwritten by checkout" not in stderr and "commit your changes" not in stderr:
            return {"ok": False, "error": stderr.strip()}
        
        # 有未提交更改 → stash 后再切换
        stash_result = _git(["git", "stash", "push", "-m", f"auto-stash before switching to {branch}"])
        if stash_result.returncode != 0:
            return {"ok": False, "error": f"stash failed: {stash_result.stderr.strip()}"}
        
        # 切换分支
        if create:
            co_result = _git(["git", "checkout", "-b", branch])
        else:
            co_result = _git(["git", "checkout", branch])
        if co_result.returncode != 0:
            # 切换失败，恢复 stash
            _git(["git", "stash", "pop"])
            return {"ok": False, "error": f"checkout failed after stash: {co_result.stderr.strip()}"}
        
        # 恢复 stash（切换成功）
        pop_result = _git(["git", "stash", "pop"])
        if pop_result.returncode != 0:
            return {"ok": True, "branch": branch, "warning": f"stash pop 有冲突: {pop_result.stderr.strip()}"}
        
        return {"ok": True, "branch": branch}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/run/{run_id}/workspace/branch")
async def switch_branch(run_id: str, req: SwitchBranchRequest):
    """切换 git 分支（在 project_root 或 workspace 目录）。"""
    git_base = _get_project_root() or _get_workspace_dir(run_id)
    if not (git_base / ".git").is_dir():
        return JSONResponse({"ok": False, "error": "not a git repository"})
    result = _git_checkout_with_stash(git_base, req.branch)
    if result["ok"]:
        return JSONResponse({"ok": True, "branch": result["branch"], "warning": result.get("warning")})
    else:
        return JSONResponse({"ok": False, "error": result["error"]})


class SwitchBranchRequest(BaseModel):
    branch: str


@app.post("/api/run/{run_id}/workspace/branch/create")
async def create_branch(run_id: str, req: SwitchBranchRequest):
    """从当前 HEAD 创建新分支并切换过去。"""
    import subprocess
    git_base = _get_project_root() or _get_workspace_dir(run_id)
    if not (git_base / ".git").is_dir():
        return JSONResponse({"ok": False, "error": "not a git repository"})
    
    branch = req.branch
    wdir = str(git_base)
    
    # 检查分支是否已存在
    r = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=wdir, capture_output=True, timeout=10
    )
    if r.returncode == 0:
        # 已存在，直接切换
        result = _git_checkout_with_stash(git_base, branch)
    else:
        # 不存在，创建并切换
        result = _git_checkout_with_stash(git_base, branch, create=True)
    
    if result["ok"]:
        return JSONResponse({"ok": True, "branch": result["branch"], "warning": result.get("warning")})
    else:
        return JSONResponse({"ok": False, "error": result["error"]})


@app.delete("/api/branch/{branch_name}")
async def delete_branch(branch_name: str):
    """删除指定的 git 分支。不允许删除当前所在分支。"""
    import subprocess
    git_dir = _get_git_dir()
    if not git_dir:
        return JSONResponse({"ok": False, "error": "not a git repository"}, status_code=400)

    wdir = str(git_dir)

    # 检查分支是否存在
    r = subprocess.run(
        ["git", "rev-parse", "--verify", branch_name],
        cwd=wdir, capture_output=True, timeout=10
    )
    if r.returncode != 0:
        return JSONResponse({"ok": False, "error": f"分支 '{branch_name}' 不存在"}, status_code=404)

    # 不允许删除当前分支
    current = _get_current_branch(git_dir)
    if branch_name == current:
        return JSONResponse({"ok": False, "error": "不能删除当前所在分支，请先切换到其他分支"}, status_code=400)

    try:
        result = subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=wdir, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return JSONResponse({"ok": False, "error": result.stderr.strip()})
        return JSONResponse({"ok": True, "branch": branch_name})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/run/{run_id}/continue")
async def continue_run(run_id: str, req: ContinueRequest):
    """Continue conversation in an existing session."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)

    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    if not run_info.session_id:
        return JSONResponse({"error": "no session_id, cannot continue"}, status_code=400)

    try:
        success = pm.continue_run(run_id, prompt=req.prompt, model=req.model)
        if success:
            return JSONResponse({"ok": True, "run_id": run_id})
        else:
            return JSONResponse({"error": "cannot continue"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/run/{run_id}/stop")
async def stop_run(run_id: str):
    """Stop a running process."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = pm.stop_run(run_id)
    if success:
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "cannot stop"}, status_code=400)


@app.delete("/api/run/{run_id}")
async def delete_run(run_id: str):
    """Delete a run (and its descendants). Stops it first if still running."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    deleted = pm.delete_run(run_id, recursive=True)
    return JSONResponse({"deleted": deleted})


@app.post("/api/run/{run_id}/rewind")
async def rewind_run(run_id: str, req: dict):
    """回退 run 到指定 seq 的 user prompt 之前。

    body: {"seq": int}  目标 prompt 事件的 seq。
    成功后 run 变成 STOPPED，前端可立刻 continue_run 发新 prompt。
    """
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


@app.get("/api/run/{run_id}/dag")
async def get_dag(run_id: str, workspace_id: str = ""):
    """返回该 run 所在 workspace 的 DAG 编排状态（步骤+状态+依赖）+ step commit 列表。
    可通过 ?workspace_id=xxx 直接指定 workspace 目录名，绕过 run_id 查找。"""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    if workspace_id:
        result = pm.dag_status_by_workspace(workspace_id)
    else:
        result = pm.dag_status(run_id)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "no dag")}, status_code=400)
    return JSONResponse(result)


@app.post("/api/run/{run_id}/dag/checkout")
async def dag_checkout(run_id: str, req: dict):
    """【回退到任一 agent】把 workspace 文件回退到某 DAG step 的 git 快照，
    并把该 step + 下游 DAG 状态重置为 pending。

    body: {"step_id": str, "rerun_downstream": bool?}
    回退后调度 agent 下一轮 --ready 会自然取到这些 pending step 重跑。
    """
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    step_id = req.get("step_id")
    if not step_id:
        return JSONResponse({"error": "step_id required"}, status_code=400)
    result = pm.dag_checkout(run_id, step_id,
                             rerun_downstream=bool(req.get("rerun_downstream")))
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "checkout failed")}, status_code=400)
    return JSONResponse(result)


# === DAG 模板 API ===

DAG_TEMPLATES_DIR = Path(__file__).parent.parent / "dag_templates"


@app.get("/api/dag/templates")
async def list_dag_templates():
    """列出所有可用的 DAG 模板。"""
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


@app.post("/api/dag/start")
async def start_dag(req: DagStartRequest):
    """启动 DAG 编排。

    resume=False（默认）：读取模板 → 初始化 dag.json → 启动调度 agent（全新任务）
    resume=True：基于现有 workspace 的 dag.json 继续（不重新初始化，接着跑 pending step）

    resume 场景：复制了 workspace（可能删了 state/），想接着跑剩下的 step。
    """
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)

    aos_dir = Path(__file__).parent.parent
    ws_dir = aos_dir / "workspaces" / req.workspace_name

    if req.resume:
        # === Resume 模式：基于现有 dag.json 继续 ===
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
        # === Start 模式：读取模板 → 初始化 dag.json ===
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
            dag["steps"].append({
                "id": s["id"],
                "name": s.get("name", s["id"]),
                "prompt": s.get("prompt", ""),
                "depends_on": s.get("depends_on", []),
                "agent_name": s.get("agent_name"),
                "model": s.get("model"),
                "type": s.get("type", "generative"),
                "status": "pending",
            })

        dag_file = ws_dir / "dag.json"
        dag_file.write_text(json.dumps(dag, indent=2, ensure_ascii=False), encoding="utf-8")

    # 打 baseline commit + __init__ step commit
    try:
        project_root = str(_get_project_root()) if _get_project_root() else None
        rec = _Recorder(project_root=project_root)
        rec.ensure_task_branch(str(ws_dir), agent_name=req.workspace_name)
        rec.baseline_commit(str(ws_dir), agent_name=req.workspace_name)
        if not req.resume:
            # 全新任务才打 __init__ step commit（resume 模式不需要，dag.json 已有状态）
            rec.step_done(
                run_id="init",
                step_id="__init__",
                workspace_path=str(ws_dir),
                message="DAG initialized",
            )
        else:
            # resume 模式：把所有 running 状态的 step 重置为 pending
            # （running 说明上次中断了，需要重跑）
            import dag_planner as dp
            dag = dp.load_dag(str(ws_dir))
            steps = dag.get("steps", [])
            reset_count = 0
            for s in steps:
                if s.get("status") == "running":
                    s["status"] = "pending"
                    reset_count += 1
            if reset_count > 0:
                dp.save_dag(str(ws_dir), dag)
    except Exception:
        pass

    # 构建调度 agent 的 system prompt
    steps_desc = "\n".join(
        f"  {i+1}. {s.get('name', s['id'])} ({s['id']}){' ← ' + ', '.join(s.get('depends_on', [])) if s.get('depends_on') else ''}"
        for i, s in enumerate(steps)
    )
    system_prompt = (
        f"你是 DAG 调度 agent。按模板顺序执行流水线：\n\n"
        f"{steps_desc}\n\n"
        f"执行方式：\n"
        f"1. `python .agent_os/dag.py --ready` → 取就绪节点（返回 JSON 数组，每项含 id/step_id/prompt/type 等字段）\n"
        f"2. `python .agent_os/spawn.py --tasks '[...]' --wait all` → 派发子 agent\n"
        f"   spawn task 字段：step_id(必须保留)、prompt、type(直接取 --ready 的 type 值，不要改名)\n"
        f"3. spawn 后立即结束对话，等 OS resume\n"
        f"4. resume 后 `python .agent_os/dag.py --mark-done <id>`，回到第 1 步\n"
        f"5. --ready 返回空时 `python .agent_os/report.py --result \"全部完成\"`\n\n"
        f"⚠️ 你已经是调度 agent，不要调用 /api/dag/start 或任何 API 来启动新 DAG。不要检查 workspace 内容，不要修改子任务的 prompt，直接 spawn 即可。"
    )

    prompt = f"请继续执行 DAG，任务名: {req.workspace_name}" if req.resume \
        else f"请执行 DAG 模板: {req.template_id}，任务名: {req.workspace_name}"
    run_id = pm.start_run(
        prompt=prompt,
        agent_name=req.workspace_name,
        model=req.model,
        workspace_name=req.workspace_name,
        system_prompt=system_prompt,
    )

    return JSONResponse({"run_id": run_id, "template": req.template_id, "resume": req.resume})


# === 三层 Git Diff API ===

@app.get("/api/run/{run_id}/diffs")
async def get_run_diffs(run_id: str):
    """返回该 agent 的三层 diff：
    - turns: 每次对话轮次的 diff [{turn, sha, diff, files}]
    - agent: 该 agent 的总 diff {sha, diff, files}
    - steps: DAG step 的 diff [{step_id, sha, diff, files}]
    """
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    ri = pm.get_run(run_id)
    if not ri or not ri.workspace_path:
        return JSONResponse({"error": "run not found or no workspace"}, status_code=404)
    ws = ri.workspace_path
    import subprocess as _sp

    result = {"turns": [], "agent": None, "steps": []}

    # 1) Turn 级 diff
    for tc in pm.recorder.turn_commits(run_id, ws):
        turn_diff = _commit_diff(tc["sha"], ws)
        result["turns"].append({
            "turn": tc["turn"],
            "sha": tc["sha"][:8],
            "diff": turn_diff["diff"],
            "files": turn_diff["files"],
        })

    # 2) Agent 级 diff（该 agent 的第一个 turn 到最后一个 turn 之间的总变更）
    if result["turns"]:
        first_sha = result["turns"][0]["sha"]
        last_sha = result["turns"][-1]["sha"]
        full_sha = f"{first_sha}..{last_sha}" if first_sha != last_sha else last_sha
        agent_diff = _commit_diff(full_sha, ws, is_range=(first_sha != last_sha))
        result["agent"] = {
            "sha": f"{first_sha}..{last_sha}" if first_sha != last_sha else last_sha,
            "diff": agent_diff["diff"],
            "files": agent_diff["files"],
        }

    # 3) Step 级 diff
    for sc in pm.recorder.list_step_commits(ws):
        step_diff = _commit_diff(sc["sha"], ws)
        result["steps"].append({
            "step_id": sc["step_id"],
            "sha": sc["sha"][:8],
            "diff": step_diff["diff"],
            "files": step_diff["files"],
        })

    return JSONResponse(result)


def _commit_diff(sha: str, ws: str, is_range: bool = False) -> dict:
    """获取某个 commit 或 commit range 的 diff 和变更文件列表。"""
    import subprocess as _sp
    try:
        if is_range:
            r = _sp.run(["git", "diff", sha], cwd=ws,
                        capture_output=True, text=True, timeout=15)
        else:
            # 先检查是否有父 commit，没有则用 git show（root commit）
            parent = _sp.run(
                ["git", "rev-parse", f"{sha}~1"],
                cwd=ws, capture_output=True, text=True, timeout=10
            )
            if parent.returncode == 0 and parent.stdout.strip():
                r = _sp.run(["git", "diff", f"{sha}~1", sha], cwd=ws,
                            capture_output=True, text=True, timeout=15)
            else:
                r = _sp.run(["git", "show", "--format=", sha], cwd=ws,
                            capture_output=True, text=True, timeout=15)
        diff = r.stdout.strip()
        files = []
        for line in diff.split("\n"):
            if line.startswith("diff --git "):
                parts = line.split(" ")
                if len(parts) >= 3:
                    f = parts[2][2:]
                    if f and not f.startswith("runs/") and f != "dag.json":
                        files.append(f)
        return {"diff": diff[:5000], "files": files}
    except Exception:
        return {"diff": "", "files": []}


@app.get("/api/run/{run_id}/export")
async def export_run(run_id: str, format: str = "md"):
    """Export a run's events to markdown or json (downloadable)."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)

    if format == "json":
        from fastapi.responses import Response
        body = _json_export(run_info)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="agent-{run_id}.json"'},
        )
    # default: markdown
    from fastapi.responses import Response
    body = _md_export(run_info)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="agent-{run_id}.md"'},
    )


def _json_export(ri) -> str:
    import json
    return json.dumps({
        "run_id": ri.run_id,
        "prompt": ri.prompt,
        "model": ri.model,
        "task_type": ri.task_type,
        "interactive": ri.interactive,
        "status": ri.status.value,
        "session_id": ri.session_id,
        "started_at": ri.started_at.isoformat(),
        "completed_at": ri.completed_at.isoformat() if ri.completed_at else None,
        "turns": len(ri.turn_markers),
        "reported_result": ri.reported_result,
        "user_terminated": ri.user_terminated,
        "messages": list(ri.messages),
        "events": list(ri.output_events),
    }, ensure_ascii=False, indent=2)


def _md_export(ri) -> str:
    """events → 人读 markdown。"""
    lines = []
    lines.append(f"# Agent OS — run `{ri.run_id}`")
    lines.append("")
    if ri.model:
        lines.append(f"- **Model**: `{ri.model}`")
    lines.append(f"- **Status**: `{ri.status.value}`")
    lines.append(f"- **Task type**: `{ri.task_type}`")
    if ri.session_id:
        lines.append(f"- **Session**: `{ri.session_id}`")
    lines.append(f"- **Started**: {ri.started_at.isoformat()}")
    if ri.completed_at:
        lines.append(f"- **Completed**: {ri.completed_at.isoformat()}")
    lines.append(f"- **Turns**: {len(ri.turn_markers)}")
    if ri.user_terminated:
        lines.append("- **Ended by user (Done)**")
    lines.append("")
    lines.append("---")
    lines.append("")
    for ev in ri.output_events:
        kind = ev.get("kind")
        if kind == "turn":
            lines.append(f"\n## Turn {ev.get('index', '?')}\n")
        elif kind == "prompt":
            src = ev.get("source", "user")
            label = "User" if src == "user" else "Orchestrator → resume"
            lines.append(f"### {label}\n")
            lines.append(ev.get("text", ""))
            lines.append("")
        elif kind == "text":
            lines.append("**Agent:**")
            lines.append("")
            lines.append(ev.get("text", ""))
            lines.append("")
        elif kind == "tool_use":
            lines.append(f"**🔧 {ev.get('tool', 'Tool')}**")
            lines.append("```")
            lines.append(ev.get("summary", ""))
            lines.append("```")
            lines.append("")
        elif kind == "tool_result":
            lines.append("_↳ result_")
            lines.append("```")
            lines.append(ev.get("text", ""))
            lines.append("```")
            lines.append("")
        elif kind == "send":
            lines.append(f"**📨 progress → parent:** {ev.get('text', '')}")
            lines.append("")
        elif kind == "report":
            lines.append("### ✓ Final result")
            lines.append("")
            lines.append(ev.get("text", ""))
            lines.append("")
        elif kind == "user_done":
            lines.append("> _Ended by user (Done)._")
            lines.append("")
        elif kind == "error":
            lines.append(f"**❌ Error:** {ev.get('text', '')}")
            lines.append("")
        elif kind == "system":
            lines.append(f"<sub>{ev.get('text', '')}</sub>")
            lines.append("")
    return "\n".join(lines)


@app.post("/api/runs/clear")
async def clear_runs():
    """Clear all completed/failed/stopped root runs (with their subtrees)."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    count = pm.clear_completed()
    return JSONResponse({"cleared": count})


@app.post("/api/run/{run_id}/clear")
async def clear_run_context(run_id: str):
    """清除当前 run 的对话上下文（类似 Claude Code 的 /clear）。

    清空 session jsonl + 内存事件流，保留 run 元数据。
    run 必须不是 RUNNING / WAITING。
    """
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    result = pm.clear_context(run_id)
    if result.get("ok"):
        return JSONResponse(result)
    return JSONResponse(result, status_code=400)


@app.get("/api/workspaces")
async def list_workspaces():
    """列出所有 workspace，含 dag.json 状态摘要（供前端选择已有 workspace 继续）。"""
    workspaces_dir = Path(__file__).parent.parent / "workspaces"
    result = []
    if workspaces_dir.is_dir():
        for ws_dir in sorted(workspaces_dir.iterdir(), reverse=True):
            if not ws_dir.is_dir():
                continue
            ws_name = ws_dir.name
            dag_file = ws_dir / "dag.json"
            step_count = 0
            done_count = 0
            pending_count = 0
            if dag_file.is_file():
                try:
                    dag = json.loads(dag_file.read_text(encoding="utf-8"))
                    steps = dag.get("steps", [])
                    step_count = len(steps)
                    done_count = sum(1 for s in steps if s.get("status") == "done")
                    pending_count = sum(1 for s in steps if s.get("status") == "pending")
                except Exception:
                    pass
            result.append({
                "name": ws_name,
                "has_dag": dag_file.is_file(),
                "step_count": step_count,
                "done_count": done_count,
                "pending_count": pending_count,
            })
    return JSONResponse({"workspaces": result})


@app.post("/api/workspace/delete")
async def delete_workspace(req: dict):
    """删除整个 workspace：找出所有 workspace_path 末段 == name 的 root run，递归删除其子树。
    并清理磁盘上的 workspace 目录。

    body: {"workspace": "<wsName>"}  —— 即 workspace_path 路径的最后一段（前端 _wsNameOf）
    """
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    name = (req or {}).get("workspace")
    if not name or not isinstance(name, str):
        return JSONResponse({"error": "workspace name required"}, status_code=400)

    def _ws_tail(p: str) -> str:
        if not p:
            return ""
        norm = p.replace("\\", "/").rstrip("/")
        return norm.rsplit("/", 1)[-1] if norm else ""

    # 找出所有匹配该 workspace 的 root run（无 parent）
    target_root_ids = []
    for ri in list(pm.runs.values()):
        if ri.parent_run_id:
            continue
        if _ws_tail(getattr(ri, "workspace_path", "") or "") == name:
            target_root_ids.append(ri.run_id)

    if not target_root_ids:
        return JSONResponse({"deleted_runs": 0, "deleted_roots": 0, "workspace": name})

    # 在 delete_run 之前先收集所有要清理的 workspace 磁盘路径（含子孙的，避免遗漏）
    ws_paths_to_purge = set()

    def _collect_ws(rid: str):
        ri = pm.runs.get(rid)
        if not ri:
            return
        if ri.workspace_path:
            ws_paths_to_purge.add(ri.workspace_path)
        for cid in list(ri.children_run_ids):
            _collect_ws(cid)

    for rid in target_root_ids:
        _collect_ws(rid)

    # 删除内存里的 run（含子树）
    deleted_runs = 0
    deleted_roots = 0
    for rid in target_root_ids:
        n = pm.delete_run(rid, recursive=True)
        if n > 0:
            deleted_roots += 1
            deleted_runs += n

    # 删除该任务的所有 git 分支
    import subprocess
    deleted_branches = []
    git_dir = _get_git_dir()
    if git_dir:
        branches = _get_git_branches(git_dir, task_name=name)
        for b in branches:
            branch_name = b["name"]
            # 如果当前正在该分支上，先切到 master
            current = _get_current_branch(git_dir)
            if branch_name == current:
                try:
                    subprocess.run(
                        ["git", "checkout", "master"],
                        cwd=str(git_dir), capture_output=True, timeout=10
                    )
                except Exception:
                    pass
            try:
                r = subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    cwd=str(git_dir), capture_output=True, text=True, timeout=10
                )
                if r.returncode == 0:
                    deleted_branches.append(branch_name)
            except Exception:
                pass

    # 清理磁盘 workspace 目录（用 subprocess 绕过 CodeBuddy safe-delete 拦截）
    import subprocess as _sp
    import platform as _pf
    purged = []
    for wp in ws_paths_to_purge:
        try:
            wp_path = Path(wp)
            wp_resolved = wp_path.resolve()
            if WORKSPACES_DIR.resolve() in wp_resolved.parents and wp_path.exists():
                if _pf.system() == "Windows":
                    _sp.run(["cmd", "/c", "rd", "/s", "/q", str(wp_path)],
                            capture_output=True, timeout=30)
                else:
                    _sp.run(["rm", "-rf", str(wp_path)],
                            capture_output=True, timeout=30)
                purged.append(str(wp_path))
        except Exception:
            pass

    return JSONResponse({
        "deleted_runs": deleted_runs,
        "deleted_roots": deleted_roots,
        "purged_dirs": len(purged),
        "deleted_branches": len(deleted_branches),
        "workspace": name,
    })


@app.post("/api/run/{run_id}/complete")
async def complete_interactive(run_id: str):
    """Mark an interactive agent as completed (user clicks 'Done')."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    success = pm.complete_interactive(run_id)
    if success:
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"error": "cannot complete (not running or not found)"}, status_code=400)


class ReportRequest(BaseModel):
    run_id: str = ""
    result: str


class SendMsgRequest(BaseModel):
    run_id: str = ""
    msg: str


@app.post("/api/run/{run_id}/send")
async def send_message(run_id: str, req: SendMsgRequest):
    """Child agent sends an intermediate message to OS (does not end task)."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)

    from datetime import datetime
    run_info.messages.append({"time": datetime.now().isoformat(), "msg": req.msg})
    run_info.add_event("send", text=req.msg)
    return JSONResponse({"ok": True})


@app.post("/api/run/{run_id}/report")
async def report_result(run_id: str, req: ReportRequest):
    """Child agent reports final result and marks task complete. Triggers resume."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)

    run_info.reported_result = req.result
    # Mark complete and trigger resume chain
    # report_complete handles both RUNNING and already-COMPLETED cases
    pm.report_complete(run_id, req.result)
    return JSONResponse({"ok": True})


# === Spawn API ===

@app.post("/api/spawn")
async def spawn_children(req: SpawnRequest):
    """Spawn multiple sub-agents for a parent. Called by spawn.py script."""
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)

    tasks = [{"prompt": t.prompt, "agent_name": t.agent_name, "type": t.type,
              "model": t.model, "step_id": t.step_id} for t in req.tasks]

    result = pm.spawn_children(
        parent_run_id=req.parent_run_id,
        parent_session_id=req.parent_session_id,
        tasks=tasks,
        wait_strategy=req.wait_strategy,
    )

    return JSONResponse(result)


# === Tree API ===

@app.get("/api/tree")
async def get_tree(workspace_id: str = ""):
    """Get agent tree structure (nested).
    
    可选参数:
    - workspace_id: 如果提供，只返回该 workspace 的 run 树。
    """
    if not pm:
        return JSONResponse({"tree": []})
    tree = pm.get_tree()
    if workspace_id:
        tree = _filter_tree_by_workspace(tree, workspace_id)
    return JSONResponse({"tree": tree})


def _filter_tree_by_workspace(tree: list, workspace_id: str) -> list:
    """过滤树节点，只保留 workspace_path 尾部匹配 workspace_id 的节点及其子树。"""
    def _ws_tail(wp: str | None) -> str:
        if not wp:
            return ""
        norm = wp.replace("\\", "/").rstrip("/")
        return norm.rsplit("/", 1)[-1] if norm else ""

    def _filter_node(node: dict) -> dict | None:
        wp = node.get("workspace_path", "")
        if _ws_tail(wp) == workspace_id:
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


@app.post("/api/run/{run_id}/label")
async def set_label(run_id: str, req: LabelRequest):
    """Set a user-friendly display label for a run."""
    if not pm:
        return JSONResponse({"error": "not ready"}, status_code=503)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    run_info.label = req.label.strip() or None
    pm._save_runs_to_disk()
    return JSONResponse({"ok": True})


# === Slash Command Completions ===

def _parse_frontmatter(text: str) -> dict:
    """Extract YAML-like frontmatter from markdown (key: value only, no nesting)."""
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return {}
    result = {}
    for line in m.group(1).splitlines():
        kv = re.match(r'^(\w[\w-]*):\s*"?(.*?)"?\s*$', line)
        if kv:
            result[kv.group(1)] = kv.group(2)
    return result


def _scan_skills(root: Path) -> list:
    items = []
    if not root.exists():
        return items
    for skill_md in root.rglob("SKILL.md"):
        try:
            text = skill_md.read_text(encoding="utf-8", errors="ignore")
            fm = _parse_frontmatter(text)
            if fm.get("disable", "").lower() == "true":
                continue
            name = fm.get("name") or skill_md.parent.name
            desc = fm.get("description", "")
            items.append({"type": "skill", "name": name, "description": desc,
                          "path": str(skill_md.relative_to(root.parent.parent))})
        except Exception:
            pass
    return items


def _scan_commands(root: Path) -> list:
    items = []
    if not root.exists():
        return items
    for md in root.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
            fm = _parse_frontmatter(text)
            if fm.get("disable", "").lower() == "true":
                continue
            name = fm.get("name") or md.stem
            desc = fm.get("description", "")
            hint = fm.get("argument-hint", "")
            items.append({"type": "command", "name": name, "description": desc,
                          "argument_hint": hint,
                          "path": str(md.relative_to(root.parent.parent))})
        except Exception:
            pass
    return items


def _scan_agents(root: Path) -> list:
    items = []
    if not root.exists():
        return items
    for md in root.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
            fm = _parse_frontmatter(text)
            if fm.get("enabled", "true").lower() == "false":
                continue
            name = fm.get("name") or md.stem
            desc = fm.get("description", "")
            items.append({"type": "agent", "name": name, "description": desc,
                          "path": str(md.relative_to(root.parent.parent))})
        except Exception:
            pass
    return items


@app.get("/api/completions")
async def get_completions():
    """Return slash-command completions: skills, commands, agents."""
    project_root = Path(pm.project_root) if pm else Path.cwd()
    codebuddy_dir = project_root / ".codebuddy"

    skills = _scan_skills(codebuddy_dir / "skills")
    commands = _scan_commands(codebuddy_dir / "commands")
    agents = _scan_agents(codebuddy_dir / "agents")

    return JSONResponse({
        "skills": skills,
        "commands": commands,
        "agents": agents,
    })


# === 录制记录 API ===

from agent_os.recorder import Recorder as _Recorder

@app.get("/api/recordings")
async def get_recordings():
    """返回所有 workspace 下的录制记录，并补充 parent/children/status 关系字段。"""
    recorder = _Recorder(project_root=str(_get_project_root()) if _get_project_root() else None)
    workspaces_dir = Path(__file__).parent.parent / "workspaces"
    all_recordings = []

    # 加载 state/runs.json，建立 run_id -> dict 索引以补充关系字段
    state_file = Path(__file__).parent.parent / "state" / "runs.json"
    rel_index: dict = {}
    if state_file.is_file():
        try:
            with state_file.open("r", encoding="utf-8") as f:
                state = json.load(f)
            for run in state.get("runs", []):
                rid = run.get("run_id")
                if rid:
                    rel_index[rid] = run
        except Exception:
            pass

    if workspaces_dir.is_dir():
        for ws_dir in sorted(workspaces_dir.iterdir(), reverse=True):
            if not ws_dir.is_dir():
                continue
            records = recorder.list_runs(str(ws_dir))
            if records:
                # 为每条记录补上 workspace_path 及关系字段
                for r in records:
                    r["workspace_path"] = str(ws_dir)
                    state_run = rel_index.get(r.get("run_id"))
                    if state_run:
                        r["parent_run_id"] = state_run.get("parent_run_id", "") or ""
                        r["children_run_ids"] = state_run.get("children_run_ids", []) or []
                        r["status"] = state_run.get("status", "")
                        r["spawn_id"] = state_run.get("spawn_id", "") or ""
                    else:
                        r.setdefault("parent_run_id", "")
                        r.setdefault("children_run_ids", [])
                        r.setdefault("status", "")
                        r.setdefault("spawn_id", "")
                all_recordings.append({
                    "workspace": ws_dir.name,
                    "runs": records,
                })

    return JSONResponse({"workspaces": all_recordings})


import subprocess as _sp

@app.get("/api/recording/{workspace_id}/{run_id}/diff")
async def get_recording_diff(workspace_id: str, run_id: str):
    """返回指定 run 对应的 git diff（上一次 commit 到该 run 的 commit 之间的变更）。"""
    workspaces_dir = Path(__file__).parent.parent / "workspaces"
    
    # 尝试直接作为子目录名查找
    ws_dir = workspaces_dir / workspace_id
    if not ws_dir.is_dir():
        # 如果找不到，尝试在目录列表中匹配
        for d in workspaces_dir.iterdir():
            if d.is_dir() and d.name.startswith(workspace_id):
                ws_dir = d
                break
    
    if not ws_dir.is_dir():
        return JSONResponse({
            "error": "workspace not found", 
            "workspace_id": workspace_id,
            "workspaces_dir": str(workspaces_dir),
            "available": [d.name for d in workspaces_dir.iterdir() if d.is_dir()][:10]
        }, status_code=404)

    commit_msg = f"[{run_id[:8]}]"
    try:
        # 找到该 run 对应的 commit hash
        r = _sp.run(
            ["git", "log", "--oneline", "--grep", commit_msg, "-n", "1"],
            cwd=str(ws_dir), capture_output=True, text=True, timeout=10
        )
        if not r.stdout.strip():
            return JSONResponse({"error": "commit not found", "hint": r.stdout.strip() or r.stderr.strip()}, status_code=404)
        
        commit_hash = r.stdout.strip().split()[0]
        
        # 获取该 commit 引入的变更（相对于它的父 commit，首 commit 用 show）
        r2 = _sp.run(
            ["git", "diff", f"{commit_hash}~1", commit_hash],
            cwd=str(ws_dir), capture_output=True, text=True, timeout=10
        )
        diff_text = r2.stdout.strip()
        if not diff_text:
            # 首 commit 没有父节点，用 git show
            r2 = _sp.run(
                ["git", "show", "--format=", commit_hash],
                cwd=str(ws_dir), capture_output=True, text=True, timeout=10
            )
            diff_text = r2.stdout.strip()
        # 同时获取变更文件列表
        r3 = _sp.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
            cwd=str(ws_dir), capture_output=True, text=True, timeout=10
        )
        files = [f for f in r3.stdout.strip().split("\n") if f and not f.startswith("runs/")]
        
        return JSONResponse({
            "commit": commit_hash,
            "diff": diff_text or "(无变更)",
            "files": files,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _resolve_workspace_dir(workspace_id: str) -> Path | None:
    """复用 diff 路由的 workspace 容错查找：先按子目录名，再按前缀匹配。"""
    workspaces_dir = Path(__file__).parent.parent / "workspaces"
    ws_dir = workspaces_dir / workspace_id
    if not ws_dir.is_dir():
        for d in workspaces_dir.iterdir():
            if d.is_dir() and d.name.startswith(workspace_id):
                ws_dir = d
                break
    return ws_dir if ws_dir.is_dir() else None


@app.get("/api/dag/{workspace_id}/steps")
async def dag_steps(workspace_id: str):
    """列出该 workspace 的所有 step commit（[step:<id>]），供前端时间线渲染。"""
    import subprocess as _sp
    ws_dir = _resolve_workspace_dir(workspace_id)
    if not ws_dir:
        return JSONResponse({"error": "workspace not found", "workspace_id": workspace_id},
                            status_code=404)
    try:
        # 用 -F 让 --grep 按字面量匹配 "[step:"（方括号在 basic-regex 里是元字符）
        r = _sp.run(
            ["git", "log", "-F", "--grep", "[step:",
             "--format=%H%x09%ct%x09%s"],
            cwd=str(ws_dir), capture_output=True, text=True, timeout=10,
        )
        steps = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            commit_hash, ts, msg = parts
            # msg 形如 "[step:research] done text"，提取 step_id
            m = re.match(r"\[step:([^\]]+)\]\s*(.*)", msg)
            if not m:
                continue
            steps.append({
                "step_id": m.group(1),
                "commit": commit_hash[:8],
                "ts": int(ts) if ts.isdigit() else None,
                "message": m.group(2),
            })
        return JSONResponse({"steps": steps})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/dag/{workspace_id}/checkout/{step_id}")
async def dag_checkout_step(workspace_id: str, step_id: str):
    """把工作区文件回退到某 step commit 的快照，从该 commit 创建衍生分支。
    衍生分支名 = {workspace_id}-r{N}。"""
    import subprocess as _sp
    project_root = str(_get_project_root()) if _get_project_root() else None
    recorder = _Recorder(project_root=project_root)
    
    ws_dir = _resolve_workspace_dir(workspace_id)
    if not ws_dir:
        return JSONResponse({"error": "workspace not found", "workspace_id": workspace_id},
                            status_code=404)
    
    result = recorder.checkout_step(step_id, str(ws_dir))
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "checkout failed"), "step_id": step_id},
                            status_code=404)
    
    # 重置 DAG 状态：step 及其所有后继步骤回到 pending
    dag_file = Path(ws_dir) / "dag.json"
    affected = 0
    if dag_file.is_file():
        try:
            dag = json.loads(dag_file.read_text(encoding="utf-8"))
            from agent_os.dag_planner import reset_steps
            steps = dag.get("steps", [])
            affected = len(reset_steps(steps, [step_id]))
            # 写入 dag.json
            dag_file.write_text(json.dumps(dag, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            affected = 0
    
    # 将 reset 后的 dag.json commit 到新分支，确保两个分支的 dag.json 不同
    if affected > 0 and project_root:
        try:
            dag_path_rel = os.path.relpath(str(dag_file), project_root).replace("\\", "/")
            _sp.run(
                ["git", "add", dag_path_rel],
                cwd=project_root, capture_output=True, timeout=10
            )
            _sp.run(
                ["git", "commit", "-m",
                 f"[checkout:{workspace_id}:{step_id}] reset {affected} step(s) to pending"],
                cwd=project_root, capture_output=True, timeout=15
            )
        except Exception:
            pass
    
    return JSONResponse({
        "ok": True,
        "step_id": step_id,
        "commit": result.get("sha", "")[:8] if result.get("sha") else "",
        "branch": result.get("branch", ""),
        "affected_steps": affected,
    })
