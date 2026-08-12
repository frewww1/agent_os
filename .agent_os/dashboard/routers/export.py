"""Export API — /api/agent/{id}/export, /api/completions, /api/models"""
import json
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from .deps import get_agent_os

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/models")
async def list_models(refresh: bool = False):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"models": []})
    return JSONResponse({"models": agent_os.list_models(refresh=refresh)})


@router.get("/agent/{agent_id}/export")
async def export_agent(agent_id: str, format: str = "md"):
    agent_os = get_agent_os()
    if not agent_os:
        return JSONResponse({"error": "not initialized"}, status_code=500)
    agent = agent_os.get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    if format == "json":
        body = _json_export(agent)
        return Response(content=body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="agent-{agent_id}.json"'})
    body = _md_export(agent)
    return Response(content=body, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="agent-{agent_id}.md"'})


def _json_export(agent) -> str:
    return json.dumps({
        "agent_id": agent.agent_id, "prompt": agent.prompt, "model": agent.model,
        "task_type": agent.task_type, "interactive": agent.interactive,
        "status": agent.status.value, "session_id": agent.session_id,
        "started_at": agent.started_at.isoformat(),
        "completed_at": agent.completed_at.isoformat() if agent.completed_at else None,
        "turns": len(agent.turn_markers), "reported_result": agent.reported_result,
        "user_terminated": agent.user_terminated, "messages": list(agent.messages),
        "events": list(agent.output_events),
    }, ensure_ascii=False, indent=2)


def _md_export(agent) -> str:
    lines = [f"# Agent OS — agent `{agent.agent_id}`", ""]
    if agent.model:
        lines.append(f"- **Model**: `{agent.model}`")
    lines.append(f"- **Status**: `{agent.status.value}`")
    lines.append(f"- **Task type**: `{agent.task_type}`")
    if agent.session_id:
        lines.append(f"- **Session**: `{agent.session_id}`")
    lines.append(f"- **Started**: {agent.started_at.isoformat()}")
    if agent.completed_at:
        lines.append(f"- **Completed**: {agent.completed_at.isoformat()}")
    lines.append(f"- **Turns**: {len(agent.turn_markers)}")
    if agent.user_terminated:
        lines.append("- **Ended by user (Done)**")
    lines.extend(["", "---", ""])
    for ev in agent.output_events:
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
    agent_os = get_agent_os()
    project_root = Path(agent_os.project_root) if agent_os else Path.cwd()
    codebuddy_dir = project_root / ".codebuddy"
    return JSONResponse({
        "skills": _scan_items(codebuddy_dir / "skills", "skill"),
        "commands": _scan_items(codebuddy_dir / "commands", "command"),
        "agents": _scan_items(codebuddy_dir / "agents", "agent"),
    })



