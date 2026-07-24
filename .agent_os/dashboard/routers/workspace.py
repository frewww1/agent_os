"""Workspace API 路由 — /api/workspace*, /api/run/{id}/workspace*, /api/branch/*"""
import json
import subprocess
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..models import SwitchBranchRequest
from ..git_utils import get_git_branches, get_current_branch, git_checkout_with_stash

router = APIRouter(prefix="/api", tags=["workspace"])

WORKSPACES_DIR = Path(__file__).parent.parent.parent / "workspaces"


def get_agent_os():
    from ..app import agent_os
    return agent_os


def _get_project_root() -> Path | None:
    agent_os = get_agent_os()
    if agent_os:
        return Path(agent_os.project_root)
    return None


def _get_git_dir() -> Path | None:
    root = _get_project_root()
    if root and (root / ".git").is_dir():
        return root
    return None


def _get_workspace_dir(run_id: str) -> Path:
    agent_os = get_agent_os()
    if agent_os:
        ri = agent_os.get_run(run_id)
        if ri and ri.workspace_path:
            return Path(ri.workspace_path)
    return WORKSPACES_DIR / run_id


def _safe_run(*args, **kwargs):
    from ...src.utils import safe_run
    return safe_run(*args, **kwargs)


@router.get("/workspaces")
async def list_workspaces():
    result = []
    if WORKSPACES_DIR.is_dir():
        for ws_dir in sorted(WORKSPACES_DIR.iterdir(), reverse=True):
            if not ws_dir.is_dir():
                continue
            ws_name = ws_dir.name
            dag_file = ws_dir / "dag.json"
            step_count = done_count = pending_count = 0
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
                "name": ws_name, "has_dag": dag_file.is_file(),
                "step_count": step_count, "done_count": done_count,
                "pending_count": pending_count,
            })
    return JSONResponse({"workspaces": result})


@router.post("/workspace/delete")
async def delete_workspace(req: dict):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    name = (req or {}).get("workspace")
    if not name or not isinstance(name, str):
        return JSONResponse({"error": "workspace name required"}, status_code=400)

    def _ws_tail(p: str) -> str:
        if not p:
            return ""
        norm = p.replace("\\", "/").rstrip("/")
        return norm.rsplit("/", 1)[-1] if norm else ""

    target_root_ids = []
    for ri in list(agent_os._registry.runs.values()):
        if ri.parent_run_id:
            continue
        if _ws_tail(getattr(ri, "workspace_path", "") or "") == name:
            target_root_ids.append(ri.run_id)

    if not target_root_ids:
        return JSONResponse({"deleted_runs": 0, "deleted_roots": 0, "workspace": name})

    ws_paths_to_purge = set()

    def _collect_ws(rid: str):
        ri = agent_os._registry.runs.get(rid)
        if not ri:
            return
        if ri.workspace_path:
            ws_paths_to_purge.add(ri.workspace_path)
        for cid in list(ri.children_run_ids):
            _collect_ws(cid)

    for rid in target_root_ids:
        _collect_ws(rid)

    deleted_runs = 0
    deleted_roots = 0
    for rid in target_root_ids:
        n = agent_os.delete_run(rid, recursive=True)
        if n > 0:
            deleted_roots += 1
            deleted_runs += n

    deleted_branches = []
    git_dir = _get_git_dir()
    if git_dir:
        branches = get_git_branches(git_dir, task_name=name)
        for b in branches:
            branch_name = b["name"]
            current = get_current_branch(git_dir)
            if branch_name == current:
                try:
                    _safe_run(["git", "checkout", "master"], cwd=str(git_dir),
                            capture_output=True, timeout=10)
                except Exception:
                    pass
            try:
                r = _safe_run(["git", "branch", "-D", branch_name], cwd=str(git_dir),
                            capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    deleted_branches.append(branch_name)
            except Exception:
                pass

    import platform as _pf
    purged = []
    for wp in ws_paths_to_purge:
        try:
            wp_path = Path(wp)
            wp_resolved = wp_path.resolve()
            if WORKSPACES_DIR.resolve() in wp_resolved.parents and wp_path.exists():
                if _pf.system() == "Windows":
                    _safe_run(["cmd", "/c", "rd", "/s", "/q", str(wp_path)],
                            capture_output=True, timeout=30)
                else:
                    _safe_run(["rm", "-rf", str(wp_path)], capture_output=True, timeout=30)
                purged.append(str(wp_path))
        except Exception:
            pass

    return JSONResponse({
        "deleted_runs": deleted_runs, "deleted_roots": deleted_roots,
        "purged_dirs": len(purged), "deleted_branches": len(deleted_branches),
        "workspace": name,
    })


@router.get("/run/{run_id}/workspace")
async def list_workspace_files(run_id: str):
    workspace_dir = _get_workspace_dir(run_id)
    if not workspace_dir.exists():
        return JSONResponse({"files": [], "branches": []})
    task_name = workspace_dir.name
    git_base = _get_project_root() or workspace_dir
    branches = get_git_branches(git_base, task_name=task_name)
    current_branch = get_current_branch(git_base)
    SKIP_DIRS = {".git"}
    SKIP_FILES = {".gitignore", ".gitattributes", ".gitmodules", ".gitkeep"}
    files = []
    for p in sorted(workspace_dir.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(workspace_dir).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if p.name in SKIP_FILES:
            continue
        rel = "/".join(rel_parts)
        stat = p.stat()
        files.append({"path": rel, "size": stat.st_size, "mtime": stat.st_mtime})
    return JSONResponse({
        "files": files, "branches": branches,
        "current_branch": current_branch, "task_name": task_name,
    })


@router.get("/run/{run_id}/workspace/file")
async def get_workspace_file(run_id: str, path: str):
    workspace_dir = _get_workspace_dir(run_id)
    try:
        target = (workspace_dir / path).resolve()
        target.relative_to(workspace_dir.resolve())
    except (ValueError, Exception):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    try:
        content = target.read_text(encoding="utf-8")
        return JSONResponse({"path": path, "type": "text", "content": content})
    except UnicodeDecodeError:
        import base64
        content = base64.b64encode(target.read_bytes()).decode()
        return JSONResponse({"path": path, "type": "binary", "content": content})


@router.post("/run/{run_id}/workspace/branch")
async def switch_branch(run_id: str, req: SwitchBranchRequest):
    git_base = _get_project_root() or _get_workspace_dir(run_id)
    if not (git_base / ".git").is_dir():
        return JSONResponse({"ok": False, "error": "not a git repository"})
    result = git_checkout_with_stash(git_base, req.branch)
    if result["ok"]:
        return JSONResponse({"ok": True, "branch": result["branch"], "warning": result.get("warning")})
    return JSONResponse({"ok": False, "error": result["error"]})


@router.post("/run/{run_id}/workspace/branch/create")
async def create_branch(run_id: str, req: SwitchBranchRequest):
    git_base = _get_project_root() or _get_workspace_dir(run_id)
    if not (git_base / ".git").is_dir():
        return JSONResponse({"ok": False, "error": "not a git repository"})
    branch = req.branch
    wdir = str(git_base)
    r = _safe_run(["git", "rev-parse", "--verify", branch], cwd=wdir, capture_output=True, timeout=10)
    if r.returncode == 0:
        result = git_checkout_with_stash(git_base, branch)
    else:
        result = git_checkout_with_stash(git_base, branch, create=True)
    if result["ok"]:
        return JSONResponse({"ok": True, "branch": result["branch"], "warning": result.get("warning")})
    return JSONResponse({"ok": False, "error": result["error"]})


@router.delete("/branch/{branch_name}")
async def delete_branch(branch_name: str):
    git_dir = _get_git_dir()
    if not git_dir:
        return JSONResponse({"ok": False, "error": "not a git repository"}, status_code=400)
    wdir = str(git_dir)
    r = _safe_run(["git", "rev-parse", "--verify", branch_name], cwd=wdir, capture_output=True, timeout=10)
    if r.returncode != 0:
        return JSONResponse({"ok": False, "error": f"分支 '{branch_name}' 不存在"}, status_code=404)
    current = get_current_branch(git_dir)
    if branch_name == current:
        return JSONResponse({"ok": False, "error": "不能删除当前所在分支"}, status_code=400)
    try:
        result = _safe_run(["git", "branch", "-D", branch_name], cwd=wdir, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return JSONResponse({"ok": False, "error": result.stderr.strip()})
        return JSONResponse({"ok": True, "branch": branch_name})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
