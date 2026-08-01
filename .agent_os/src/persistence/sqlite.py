"""Agent OS 持久化层：基于 sqlite3 的 runs 存储。

替代原有的 JSON 文件方案，提供 ACID 事务、并发安全、schema 版本控制。
"""
import json as _json
import os
import sqlite3
import logging
import threading
from datetime import datetime

from ..core.models import RunInfo, RunStatus
from ..utils import sanitize

logger = logging.getLogger("agent_os")

SCHEMA_VERSION = 1

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL DEFAULT '_global',
    status TEXT NOT NULL DEFAULT 'running',
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_runs_workspace ON runs(workspace);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
"""


def _get_db_path(pm) -> str:
    """返回 sqlite 数据库文件路径。"""
    return os.path.join(pm._state_dir, "runs.db")


def _get_connection(pm) -> sqlite3.Connection:
    """获取数据库连接（线程安全，自动创建表和索引）。"""
    db_path = _get_db_path(pm)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(_CREATE_TABLE)
    conn.execute("PRAGMA user_version")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(_CREATE_INDEX)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def serialize_run(ri: RunInfo) -> dict:
    """把 RunInfo 转成可 JSON 化的字典（使用 pydantic 模型方法）。"""
    return sanitize(ri.to_jsonable())


def save_runs_to_disk(pm) -> None:
    """保存所有 runs 到 sqlite 数据库。

    使用 UPSERT 语义：已存在的 run_id 更新，不存在的插入。
    数据库写入天然原子，不再需要 tmp 文件 + os.replace。
    保存后执行 WAL checkpoint 确保数据落盘到主 DB 文件。
    """
    try:
        conn = pm._db_conn if hasattr(pm, '_db_conn') and pm._db_conn else _get_connection(pm)
        if not hasattr(pm, '_db_conn'):
            pm._db_conn = conn

        now = datetime.now().isoformat()
        saved = 0
        with conn:
            for ri in pm.runs.values():
                ws = ri.workspace_path or "_global"
                data = serialize_run(ri)
                conn.execute(
                    """INSERT INTO runs (run_id, workspace, status, data, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                           workspace=excluded.workspace,
                           status=excluded.status,
                           data=excluded.data,
                           updated_at=excluded.updated_at""",
                    (ri.run_id, ws, ri.status.value, _json.dumps(data, ensure_ascii=False), now)
                )
                saved += 1
        # WAL checkpoint：确保数据写入主 DB 文件，防止重启丢失
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
        if saved > 0:
            logger.info(f"save_runs: {saved} runs persisted to sqlite")
    except Exception as e:
        logger.error(f"save_runs failed: {e}")


def _auto_resume_stalled_parents(pm) -> None:
    """重启恢复：处理因重启中断的 supervisor 相关 run。

    两种情况：
    1. WAITING 状态：agent 在等 supervisor review，但 supervisor 已终止
       → 用最后一条 CORRECTION 消息 resume agent
    2. RUNNING 状态 + 有 _active_supervisor：agent 被 continue_run resume 后
       CLI 进程因重启被杀，无人触发 _on_run_completed
       → 自动完成并触发 _on_run_completed（会检测 _active_supervisor 并 resume supervisor）
    """
    for run_id, ri in list(pm.runs.items()):
        if ri.interactive or not ri.session_id:
            continue

        if ri.status == RunStatus.RUNNING:
            active_sup = getattr(ri, '_active_supervisor', None)
            if active_sup and active_sup in pm.runs:
                logger.info(f"auto-complete RUNNING agent {run_id[:8]} (dead CLI, supervisor active)")
                ri.status = RunStatus.COMPLETED
                ri.completed_at = ri.completed_at or __import__('datetime').datetime.now()
                ri.add_event("system", text="[Agent OS] Recovered after restart")
                pm.on_run_completed(ri)
                pm._mark_dirty()
            continue

        if ri.status != RunStatus.WAITING:
            continue
        waiting_sup = getattr(ri, '_waiting_supervisor', None)
        if not waiting_sup or waiting_sup not in pm.runs:
            continue
        sup_ri = pm.runs[waiting_sup]
        if sup_ri.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                              RunStatus.ERROR, RunStatus.STOPPED,
                              RunStatus.KILLED):
            # supervisor 已终止 → 用消息列表中的最后一条 CORRECTION 作为 feedback resume 执行 agent
            last_msg = ""
            for msg in reversed(ri.messages or []):
                txt = msg.get("msg", "") if isinstance(msg, dict) else str(msg)
                if "CORRECTION" in txt.upper() or "correction" in txt.lower():
                    last_msg = txt
                    break
            if not last_msg:
                last_msg = ri.messages[-1].get("msg", "") if ri.messages and isinstance(ri.messages[-1], dict) else ""
            logger.info(f"auto-resume stalled WAITING agent {run_id[:8]} "
                         f"(supervisor={waiting_sup[:8]} {sup_ri.status.value})")
            feedback = last_msg or "CORRECTION: Please review the work"
            pm.continue_run(run_id, feedback, source="os")
            pm._mark_dirty()


def _restore_events_with_jsonl(pm, ri, r, os_events_backup: list) -> None:
    """从 jsonl 恢复会话事件，合并 OS 注入事件。jsonl 由 CodeBuddy CLI 维护。"""
    os_events = r.get("os_events", os_events_backup)
    try:
        cwd = ri.workspace_path or pm.project_root
        jsonl_path = pm._backend.get_session_path(ri.session_id, cwd) if ri.session_id else None
    except Exception as e:
        logger.warning(f"restore jsonl: get_session_path failed for {ri.run_id[:8]}: {e}")
        jsonl_path = None

    events: list[dict] = []

    # 1. 从 jsonl 读取会话事件
    # CodeBuddy 用 session 格式，Claude 用 stream-json 格式，依次尝试
    if jsonl_path and os.path.exists(jsonl_path):
        try:
            from .session_parser import parse_cli_session_jsonl
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                seq = 1
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parsed = parse_cli_session_jsonl(line)
                    if not parsed:
                        try:
                            from ..agent.stream_parser import parse_stream_json_events
                            parsed = parse_stream_json_events(line)
                        except Exception:
                            pass
                    for ev in parsed:
                        ev["seq"] = seq
                        ev["_src"] = "jsonl"
                        events.append(ev)
                        seq += 1
        except Exception as e:
            logger.warning(f"restore jsonl parse for {ri.run_id[:8]}: {e}")

    # 2. 加入 OS 注入事件（system/error/turn/send/rewind/user_done）
    for e in os_events:
        e["_src"] = "os"
        events.append(e)

    # 3. 按 seq 排序后写入 output_events
    events.sort(key=lambda e: e.get("seq", 0))
    for e in events:
        ri.output_events.append(e)
    ri._event_seq = max(ri._event_seq, max((e.get("seq", 0) for e in events), default=0))
    if events:
        logger.info(f"restored {len(events)} events for {ri.run_id[:8]} "
                     f"({len(os_events)} OS, jsonl={jsonl_path is not None})")


def load_runs_from_disk(pm) -> None:
    """启动时从 sqlite 恢复历史 runs。

    同时兼容旧 JSON 文件格式：如果数据库为空但有旧的 JSON 文件，
    自动从 JSON 迁移到 sqlite。
    """
    conn = _get_connection(pm)
    pm._db_conn = conn

    # 尝试从旧 JSON 文件迁移
    _migrate_from_json(pm, conn)

    seen_run_ids: set[str] = set()
    total = 0
    try:
        rows = conn.execute(
            "SELECT run_id, workspace, status, data FROM runs ORDER BY updated_at"
        ).fetchall()

        for row in rows:
            rid = row["run_id"]
            if not rid or rid in seen_run_ids:
                continue
            seen_run_ids.add(rid)
            status_str = row["status"]
            if status_str == "waiting":
                pm._originally_waiting.add(rid)
            if status_str in ("running", "waiting"):
                status_str = "stopped"

            try:
                r = _json.loads(row["data"])
                r = sanitize(r)
            except (_json.JSONDecodeError, TypeError):
                logger.warning(f"load_runs: corrupted data for {rid}")
                continue

            ri = RunInfo(
                run_id=rid,
                prompt=r.get("prompt", ""),
                status=RunStatus(status_str),
                interactive=r.get("interactive", False),
                session_id=r.get("session_id"),
                model=r.get("model"),
                task_type=r.get("task_type", "generative"),
                reported_result=r.get("reported_result"),
                user_terminated=r.get("user_terminated", False),
                messages=r.get("messages", []),
                parent_run_id=r.get("parent_run_id"),
                children_run_ids=r.get("children_run_ids", []),
                started_at=datetime.fromisoformat(r["started_at"]),
                completed_at=datetime.fromisoformat(r["completed_at"]) if r.get("completed_at") else None,
                exit_code=r.get("exit_code"),
                workspace_path=r.get("workspace_path"),
                spawn_id=r.get("spawn_id"),
                step_id=r.get("step_id"),
                system_prompt=r.get("system_prompt"),
                goal=r.get("goal"),
                goal_retries=r.get("goal_retries", 0),
                plan_content=r.get("plan_content"),
                plan_file=r.get("plan_file"),
            )
            ri._fallback_result = r.get("fallback_result")
            ri._event_seq = r.get("event_seq", 0)
            ri.turn_markers = r.get("turn_markers", [])
            # 恢复 supervisor 相关内存字段
            if r.get("waiting_supervisor"):
                object.__setattr__(ri, '_waiting_supervisor', r["waiting_supervisor"])
            if r.get("active_supervisor"):
                object.__setattr__(ri, '_active_supervisor', r["active_supervisor"])
            # 恢复 OS 注入事件并尝试合并 jsonl 会话记录
            os_events = r.get("os_events", [])
            _restore_events_with_jsonl(pm, ri, r, os_events)
            pm.runs[ri.run_id] = ri
            total += 1

    except Exception as e:
        logger.warning(f"load_runs from sqlite failed: {e}")

    logger.info(f"loaded {total} historical runs ({len(seen_run_ids)} unique) from sqlite")
    pm._registry.restore_spawn_requests()
    _auto_resume_stalled_parents(pm)


def _migrate_from_json(pm, conn: sqlite3.Connection) -> None:
    """如果 sqlite 数据库为空，尝试从旧 JSON 文件迁移数据。

    迁移源（按优先级）：
    1. state/workspaces/<ws>/runs.json（新位置分片）
    2. workspaces/<ws>/state/runs.json（旧位置分片）
    3. state/runs.json（全局聚合文件）
    """
    # 检查数据库是否已有数据
    count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    if count > 0:
        return

    candidates: list[str] = []
    # 新位置分片
    ws_state_dir = os.path.join(pm._state_dir, "workspaces")
    if os.path.isdir(ws_state_dir):
        for ws_name in os.listdir(ws_state_dir):
            ws_file = os.path.join(ws_state_dir, ws_name, "runs.json")
            if os.path.isfile(ws_file):
                candidates.append(ws_file)
    # 旧位置分片
    workspaces_dir = os.path.join(pm.project_root, ".agent_os", "workspaces")
    if os.path.isdir(workspaces_dir):
        for ws_name in os.listdir(workspaces_dir):
            old_ws_state = os.path.join(workspaces_dir, ws_name, "state", "runs.json")
            if os.path.exists(old_ws_state):
                candidates.append(old_ws_state)
    # 全局聚合文件
    if os.path.exists(pm._runs_file):
        candidates.append(pm._runs_file)

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
                    """INSERT OR IGNORE INTO runs (run_id, workspace, status, data, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (rid, ws, r.get("status", "completed"), _json.dumps(r, ensure_ascii=False), now)
                )
                migrated += 1
        except Exception as e:
            logger.warning(f"migrate from {file_path} failed: {e}")

    if migrated > 0:
        conn.commit()
        logger.info(f"migrated {migrated} runs from JSON files to sqlite")
