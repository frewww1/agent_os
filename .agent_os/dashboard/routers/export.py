"""Export + Diff + Recordings API — /api/run/{id}/export, /api/run/{id}/diffs, /api/recordings, /api/completions, /api/models"""
import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter(prefix="/api", tags=["export"])


def get_pm():
    from ..app import pm
    return pm


def _get_project_root() -> Path | None:
    pm = get_pm()
    if pm:
        return Path(pm.project_root)
    return None


def _safe_run(*args, **kwargs):
    from ...src.utils import safe_run
    return safe_run(*args, **kwargs)


@router.get("/models")
async def list_models(refresh: bool = False):
    pm = get_pm()
    if not pm:
        return JSONResponse({"models": []})
    return JSONResponse({"models": pm.list_models(refresh=refresh)})


@router.get("/run/{run_id}/diffs")
async def get_run_diffs(run_id: str):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    ri = pm.get_run(run_id)
    if not ri or not ri.workspace_path:
        return JSONResponse({"error": "run not found or no workspace"}, status_code=404)
    ws = ri.workspace_path
    result = {"turns": [], "agent": None, "steps": []}
    for tc in pm.recorder.turn_commits(run_id, ws):
        turn_diff = _commit_diff(tc["sha"], ws)
        result["turns"].append({
            "turn": tc["turn"], "sha": tc["sha"][:8],
            "diff": turn_diff["diff"], "files": turn_diff["files"],
        })
    if result["turns"]:
        first_sha = result["turns"][0]["sha"]
        last_sha = result["turns"][-1]["sha"]
        full_sha = f"{first_sha}..{last_sha}" if first_sha != last_sha else last_sha
        agent_diff = _commit_diff(full_sha, ws, is_range=(first_sha != last_sha))
        result["agent"] = {
            "sha": full_sha, "diff": agent_diff["diff"], "files": agent_diff["files"],
        }
    for sc in pm.recorder.list_step_commits(ws):
        step_diff = _commit_diff(sc["sha"], ws)
        result["steps"].append({
            "step_id": sc["step_id"], "sha": sc["sha"][:8],
            "diff": step_diff["diff"], "files": step_diff["files"],
        })
    return JSONResponse(result)


def _commit_diff(sha: str, ws: str, is_range: bool = False) -> dict:
    try:
        if is_range:
            r = _safe_run(["git", "diff", sha], cwd=ws, capture_output=True, text=True, timeout=15)
        else:
            parent = _safe_run(["git", "rev-parse", f"{sha}~1"], cwd=ws, capture_output=True, text=True, timeout=10)
            if parent.returncode == 0 and parent.stdout.strip():
                r = _safe_run(["git", "diff", f"{sha}~1", sha], cwd=ws, capture_output=True, text=True, timeout=15)
            else:
                r = _safe_run(["git", "show", "--format=", sha], cwd=ws, capture_output=True, text=True, timeout=15)
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


@router.get("/run/{run_id}/export")
async def export_run(run_id: str, format: str = "md"):
    pm = get_pm()
    if not pm:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    run_info = pm.get_run(run_id)
    if not run_info:
        return JSONResponse({"error": "run not found"}, status_code=404)
    if format == "json":
        body = _json_export(run_info)
        return Response(content=body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="agent-{run_id}.json"'})
    body = _md_export(run_info)
    return Response(content=body, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="agent-{run_id}.md"'})


def _json_export(ri) -> str:
    return json.dumps({
        "run_id": ri.run_id, "prompt": ri.prompt, "model": ri.model,
        "task_type": ri.task_type, "interactive": ri.interactive,
        "status": ri.status.value, "session_id": ri.session_id,
        "started_at": ri.started_at.isoformat(),
        "completed_at": ri.completed_at.isoformat() if ri.completed_at else None,
        "turns": len(ri.turn_markers), "reported_result": ri.reported_result,
        "user_terminated": ri.user_terminated, "messages": list(ri.messages),
        "events": list(ri.output_events),
    }, ensure_ascii=False, indent=2)


def _md_export(ri) -> str:
    lines = [f"# Agent OS — run `{ri.run_id}`", ""]
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
    lines.extend(["", "---", ""])
    for ev in ri.output_events:
        kind = ev.get("kind")
        if kind == "turn":
            lines.append(f"\n## Turn {ev.get('index', '?')}\n")
        elif kind == "prompt":
            lines.append(f"### {'User' if ev.get('source','user')=='user' else 'Orchestrator → resume'}\n")
            lines.append(ev.get("text", "")); lines.append("")
        elif kind == "text":
            lines.append("**Agent:**\n"); lines.append(ev.get("text", "")); lines.append("")
        elif kind == "tool_use":
            lines.append(f"**🔧 {ev.get('tool', 'Tool')}**\n```\n{ev.get('summary', '')}\n```\n")
        elif kind == "tool_result":
            lines.append("_↳ result_\n```\n" + ev.get("text", "") + "\n```\n")
        elif kind == "send":
            lines.append(f"**📨 progress → parent:** {ev.get('text', '')}\n")
        elif kind == "report":
            lines.append("### ✓ Final result\n\n" + ev.get("text", "") + "\n")
        elif kind == "user_done":
            lines.append("> _Ended by user (Done)._\n")
        elif kind == "error":
            lines.append(f"**❌ Error:** {ev.get('text', '')}\n")
        elif kind == "system":
            lines.append(f"<sub>{ev.get('text', '')}</sub>\n")
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> dict:
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return {}
    result = {}
    for line in m.group(1).splitlines():
        kv = re.match(r'^(\w[\w-]*):\s*"?(.*?)"?\s*$', line)
        if kv:
            result[kv.group(1)] = kv.group(2)
    return result


def _scan_items(root: Path, item_type: str) -> list:
    items = []
    if not root.exists():
        return items
    for md in root.rglob("*.md" if item_type != "skill" else "SKILL.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
            fm = _parse_frontmatter(text)
            if fm.get("disable", "").lower() == "true" or fm.get("enabled", "").lower() == "false":
                continue
            name = fm.get("name") or (md.parent.name if item_type == "skill" else md.stem)
            desc = fm.get("description", "")
            item = {"type": item_type, "name": name, "description": desc,
                    "path": str(md.relative_to(root.parent.parent))}
            if item_type == "command":
                item["argument_hint"] = fm.get("argument-hint", "")
            items.append(item)
        except Exception:
            pass
    return items


@router.get("/completions")
async def get_completions():
    pm = get_pm()
    project_root = Path(pm.project_root) if pm else Path.cwd()
    codebuddy_dir = project_root / ".codebuddy"
    return JSONResponse({
        "skills": _scan_items(codebuddy_dir / "skills", "skill"),
        "commands": _scan_items(codebuddy_dir / "commands", "command"),
        "agents": _scan_items(codebuddy_dir / "agents", "agent"),
    })


@router.get("/recordings")
async def get_recordings():
    from agent_os.src.persistence.git_recorder import Recorder as _Recorder
    recorder = _Recorder(project_root=str(_get_project_root()) if _get_project_root() else None)
    workspaces_dir = Path(__file__).parent.parent.parent / "workspaces"
    all_recordings = []
    state_file = Path(__file__).parent.parent.parent / "state" / "runs.json"
    rel_index = {}
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
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
                all_recordings.append({"workspace": ws_dir.name, "runs": records})
    return JSONResponse({"workspaces": all_recordings})


@router.get("/recording/{workspace_id}/{run_id}/diff")
async def get_recording_diff(workspace_id: str, run_id: str):
    workspaces_dir = Path(__file__).parent.parent.parent / "workspaces"
    ws_dir = workspaces_dir / workspace_id
    if not ws_dir.is_dir():
        for d in workspaces_dir.iterdir():
            if d.is_dir() and d.name.startswith(workspace_id):
                ws_dir = d
                break
    if not ws_dir.is_dir():
        return JSONResponse({"error": "workspace not found"}, status_code=404)
    commit_msg = f"[{run_id[:8]}]"
    try:
        r = _safe_run(["git", "log", "--oneline", "--grep", commit_msg, "-n", "1"],
                      cwd=str(ws_dir), capture_output=True, text=True, timeout=10)
        if not r.stdout.strip():
            return JSONResponse({"error": "commit not found"}, status_code=404)
        commit_hash = r.stdout.strip().split()[0]
        r2 = _safe_run(["git", "diff", f"{commit_hash}~1", commit_hash],
                       cwd=str(ws_dir), capture_output=True, text=True, timeout=10)
        diff_text = r2.stdout.strip()
        if not diff_text:
            r2 = _safe_run(["git", "show", "--format=", commit_hash],
                           cwd=str(ws_dir), capture_output=True, text=True, timeout=10)
            diff_text = r2.stdout.strip()
        r3 = _safe_run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
                       cwd=str(ws_dir), capture_output=True, text=True, timeout=10)
        files = [f for f in r3.stdout.strip().split("\n") if f and not f.startswith("runs/")]
        return JSONResponse({"commit": commit_hash, "diff": diff_text or "(无变更)", "files": files})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
