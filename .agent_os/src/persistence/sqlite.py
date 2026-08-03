"""Agent OS 持久化层：基于 sqlite3 的 agents 存储。"""
import json as _json
import os
import sqlite3
import logging
from datetime import datetime

from ..core.agents.base import RunStatus
from ..utils import sanitize

logger = logging.getLogger("agent_os")

SCHEMA_VERSION = 1

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL DEFAULT '_global',
    status TEXT NOT NULL DEFAULT 'running',
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_agents_workspace ON agents(workspace);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
"""


def _get_db_path(os_) -> str:
    return os.path.join(os_._state_dir, "agents.db")


def _get_connection(os_) -> sqlite3.Connection:
    db_path = _get_db_path(os_)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(_CREATE_TABLE)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(_CREATE_INDEX)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def serialize_agent(agent) -> dict:
    return sanitize(agent.to_jsonable())


def save_agents_to_disk(os_) -> None:
    try:
        if not getattr(os_, '_db_conn', None):
            os_._db_conn = _get_connection(os_)
        conn = os_._db_conn

        now = datetime.now().isoformat()
        saved = 0
        with conn:
            for agent in os_.agents.values():
                ws = agent.workspace_path or "_global"
                data = serialize_agent(agent)
                conn.execute(
                    """INSERT INTO agents (agent_id, workspace, status, data, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                           workspace=excluded.workspace,
                           status=excluded.status,
                           data=excluded.data,
                           updated_at=excluded.updated_at""",
                    (agent.agent_id, ws, agent.status.value, _json.dumps(data, ensure_ascii=False), now)
                )
                saved += 1
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        if saved > 0:
            logger.info(f"persist: saved {saved} agents")
    except Exception as e:
        logger.error(f"persist failed: {e}")


def _parse_jsonl_events(os_, agent) -> list[dict]:
    """从 jsonl 解析 CLI 事件。"""
    if not agent.session_id:
        return []
    try:
        cwd = agent.workspace_path or os_.project_root
        jsonl_path = os_._backend.get_session_path(agent.session_id, cwd)
    except Exception:
        return []
    if not jsonl_path or not os.path.exists(jsonl_path):
        return []
    events = []
    try:
        from .session_parser import parse_cli_session_jsonl
        from ..agent.stream_parser import parse_stream_json_events
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parsed = parse_cli_session_jsonl(line)
                if not parsed:
                    try:
                        parsed = parse_stream_json_events(line)
                    except Exception:
                        pass
                for ev in parsed:
                    events.append(ev)
    except Exception as e:
        logger.warning(f"parse jsonl failed for {agent.agent_id[:8]}: {e}")
    return events


def _restore_children_tracking(os_) -> None:
    for agent_id, agent in os_.agents.items():
        for child_id in agent.children_ids:
            child = os_.agents.get(child_id)
            if child:
                agent._children_completed[child_id] = child.status in (
                    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED
                )
                child.parent = agent
                agent.children.append(child)


def _auto_resume_stalled_parents(os_) -> None:
    for agent_id, agent in list(os_.agents.items()):
        if agent.interactive or not agent.session_id:
            continue
        if agent.status == RunStatus.RUNNING:
            sup = agent._supervisor_agent
            goal = agent._goal_agent
            if sup or goal:
                logger.info(f"auto-complete RUNNING agent {agent_id[:8]} (dead CLI)")
                agent.status = RunStatus.COMPLETED
                agent.completed_at = agent.completed_at or datetime.now()
                agent.add_event("system", text="[Agent OS] Recovered after restart")
                agent.on_completed()
            continue


def load_agents_from_disk(os_) -> None:
    conn = _get_connection(os_)
    os_._db_conn = conn

    _migrate_from_json(os_, conn)

    seen_ids: set[str] = set()
    total = 0
    try:
        rows = conn.execute(
            "SELECT agent_id, workspace, status, data FROM agents ORDER BY updated_at"
        ).fetchall()

        for row in rows:
            aid = row["agent_id"]
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            status_str = row["status"]
            if status_str == "waiting":
                os_._originally_waiting.add(aid)
            if status_str in ("running", "waiting"):
                status_str = "stopped"

            try:
                r = _json.loads(row["data"])
                r = sanitize(r)
            except (_json.JSONDecodeError, TypeError):
                logger.warning(f"load_agents: corrupted data for {aid}")
                continue

            from ..core.agents import Agent
            agent = Agent(
                backend=os_._backend, project_root=os_.project_root,
                agent_id=aid,
                prompt=r.get("prompt", ""),
                status=RunStatus(status_str),
                interactive=r.get("interactive", False),
                session_id=r.get("session_id"),
                model=r.get("model"),
                task_type=r.get("task_type", "generative"),
                reported_result=r.get("reported_result"),
                user_terminated=r.get("user_terminated", False),
                messages=r.get("messages", []),
                parent_id=r.get("parent_id"),
                children_ids=r.get("children_ids", []),
                started_at=datetime.fromisoformat(r["started_at"]),
                completed_at=datetime.fromisoformat(r["completed_at"]) if r.get("completed_at") else None,
                exit_code=r.get("exit_code"),
                workspace_path=r.get("workspace_path"),
                step_id=r.get("step_id"),
                system_prompt=r.get("system_prompt"),
                goal=r.get("goal"),
                goal_retries=r.get("goal_retries", 0),
                plan_content=r.get("plan_content"),
                plan_file=r.get("plan_file"),
            )
            agent.fallback_result = r.get("fallback_result")
            agent.turn_markers = r.get("turn_markers", [])
            os_events = r.get("os_events", [])
            cli_events = _parse_jsonl_events(os_, agent)
            agent.restore_events(os_events, cli_events)
            os_.agents[agent.agent_id] = agent
            total += 1

    except Exception as e:
        logger.warning(f"load_agents from sqlite failed: {e}")

    logger.info(f"loaded {total} historical agents ({len(seen_ids)} unique) from sqlite")
    _restore_children_tracking(os_)
    _auto_resume_stalled_parents(os_)


def _migrate_from_json(os_, conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    if count > 0:
        return

    candidates: list[str] = []
    ws_state_dir = os.path.join(os_._state_dir, "workspaces")
    if os.path.isdir(ws_state_dir):
        for ws_name in os.listdir(ws_state_dir):
            ws_file = os.path.join(ws_state_dir, ws_name, "runs.json")
            if os.path.isfile(ws_file):
                candidates.append(ws_file)
    workspaces_dir = os.path.join(os_.project_root, ".agent_os", "workspaces")
    if os.path.isdir(workspaces_dir):
        for ws_name in os.listdir(workspaces_dir):
            old_ws_state = os.path.join(workspaces_dir, ws_name, "state", "runs.json")
            if os.path.exists(old_ws_state):
                candidates.append(old_ws_state)
    if not candidates:
        return

    seen: set[str] = set()
    migrated = 0
    now = datetime.now().isoformat()

    for file_path in candidates:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            data = sanitize(data)
            for r in data.get("runs", []):
                rid = r.get("run_id")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                ws = r.get("workspace_path") or "_global"
                conn.execute(
                    """INSERT OR IGNORE INTO agents (agent_id, workspace, status, data, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (rid, ws, r.get("status", "completed"), _json.dumps(r, ensure_ascii=False), now)
                )
                migrated += 1
        except Exception as e:
            logger.warning(f"migrate from {file_path} failed: {e}")

    if migrated > 0:
        conn.commit()
        logger.info(f"migrated {migrated} agents from JSON files to sqlite")
