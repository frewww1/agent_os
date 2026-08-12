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


def load_full_events(os_, agent) -> list[dict]:
    """从持久化源 + 内存合并出 agent 的完整事件序列（按 ts 升序）。

    内存 output_events 是 deque(maxlen=10000)，超长会话最早的会被裁剪；
    完整历史从 DB os_events + jsonl cli_events 重建，再补充内存中
    尚未落盘的最新 OS 事件（persist 快照之后新增的）。
    """
    # 1) DB 持久化的 OS 事件（agent_os 自己的 system/turn/prompt/error 等）
    os_events: list[dict] = []
    try:
        conn = os_._db_conn or _get_connection(os_)
        row = conn.execute(
            "SELECT data FROM agents WHERE agent_id = ?", (agent.agent_id,)
        ).fetchone()
        if row:
            r = _json.loads(row["data"])
            os_events = list(r.get("os_events") or [])
    except Exception as e:
        logger.warning(f"load_full_events: read os_events failed for {agent.agent_id[:8]}: {e}")

    # 2) jsonl 里的 CLI 事件（完整历史，不受 deque 裁剪影响）
    cli_events = _parse_jsonl_events(os_, agent)

    # 3) 合并
    events: list[dict] = []
    for ev in cli_events:
        ev["_src"] = "jsonl"
        events.append(ev)
    os_ts: set[str] = set()
    for e in os_events:
        e["_src"] = "os"
        os_ts.add(e.get("ts"))
        events.append(e)
    # 内存中未落盘的最新 OS 事件（运行时 add_event 产生、persist 尚未写入的）
    _OS_KINDS = {"system", "error", "turn", "send", "rewind", "user_done"}
    for e in agent.output_events:
        if e.get("kind") in _OS_KINDS and e.get("ts") not in os_ts:
            ev = dict(e)
            ev.setdefault("_src", "os")
            events.append(ev)
    events.sort(key=lambda e: e.get("ts", ""))
    return events


def _restore_children_tracking(os_) -> None:
    """重启后重建父子关系。

    注意：Agent.children_ids 是 property（从 self.children 派生），加载后
    children 为空，不能据此恢复；改用子 agent 持久化的 parent_id 反向重建。
    """
    for agent in os_.agents.values():
        pid = agent.parent_id
        if pid and pid in os_.agents:
            parent = os_.agents[pid]
            if agent not in parent.children:
                parent.children.append(agent)
            agent.parent = parent
    for agent in os_.agents.values():
        agent._children_completed = {
            c.agent_id: c.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED)
            for c in agent.children
        }


def _auto_resume_stalled_parents(os_) -> None:
    # 重启后不自动 resume：保留 waiting/running 状态由用户决定是否继续，
    # 仅对 supervisor/goal 的 RUNNING 做保守处理（审查流程无法原地恢复，标记完成）。
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

            try:
                r = _json.loads(row["data"])
                r = sanitize(r)
            except (_json.JSONDecodeError, TypeError):
                logger.warning(f"load_agents: corrupted data for {aid}")
                continue

            status_str = row["status"]
            # waiting 保留：父 agent 等子 agent，重启后保持等待状态由用户决定是否继续
            if status_str == "running":
                if not r.get("interactive", False):
                    # 非 interactive 的 CLI 进程已随重启消亡，无法原地继续 → stopped
                    status_str = "stopped"
                # interactive 保持 running：turn 结束 CLI 退出是常态，
                # 重启后等待用户继续对话 / 点 Done

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
    # 修复：重启恢复的 agent 不会经过 start_agent，导致 _on_step_done/_on_step_start/
    # _on_child_created 回调丢失（base.py 构造时置 None）。后果：spawn 的子 agent
    # 无法注册进 os_.agents 索引（dashboard 无法交互/报表 404），DAG 步骤也无法自动更新。
    # 这里对缺失回调的 agent 重新绑定到 AgentOS 实例方法。
    for _agent in os_.agents.values():
        if not _agent._on_child_created:
            _agent._on_step_done = os_._on_agent_step_done
            _agent._on_step_start = os_._on_agent_step_start
            _agent._on_child_created = os_._register_child
            logger.info(f"[{_agent.agent_id[:8]}] re-bound step/child callbacks after restore")
    _auto_resume_stalled_parents(os_)
