"""SessionManager — 会话回退 + 上下文清空。

纯数据操作 + 文件 I/O，不关心持久化/EventBus/状态转换。
"""
import json as _json
import logging
import os
import shutil
from datetime import datetime

from ..models import RunInfo, RunStatus

logger = logging.getLogger("agent_os")


class SessionManager:
    """会话回退与清空。不持有 AgentOS 引用，仅操作 RunInfo + session 文件。"""

    def __init__(self, backend):
        self._backend = backend

    # ---- rewind ----

    def rewind_to(self, run_info: RunInfo, target_seq: int) -> dict:
        """回退会话到 seq=target_seq 的 user prompt 之前。

        返回 {"ok", "cut_seq", "jsonl_cut_line", "backup", "error", "code"}。
        code 非空代表业务错误（非系统异常），调用方应包装为错误响应。
        """
        result = self._validate_rewind(run_info, target_seq)
        if result:
            return result

        target_ev = self._find_target_event(run_info, target_seq)
        if isinstance(target_ev, dict):
            return target_ev

        jsonl_path = self._get_jsonl_path(run_info)
        if isinstance(jsonl_path, dict):
            return jsonl_path

        result = self._truncate_jsonl(run_info, target_ev, jsonl_path)
        if result:
            return result

        cut_line_idx = result["jsonl_cut_line"]
        backup = result["backup"]
        self._truncate_memory(run_info, target_seq, target_ev)
        run_info.add_event("rewind", to_seq=target_seq, jsonl_cut_line=cut_line_idx)
        return {"ok": True, "cut_seq": target_seq, "jsonl_cut_line": cut_line_idx, "backup": backup}

    def _validate_rewind(self, ri: RunInfo, target_seq: int) -> dict | None:
        if ri.status in (RunStatus.RUNNING, RunStatus.WAITING):
            return {"ok": False, "error": f"cannot rewind while status={ri.status.value}; stop the run first"}
        if not ri.session_id:
            return {"ok": False, "error": "run has no session_id"}
        return None

    def _find_target_event(self, ri: RunInfo, target_seq: int) -> dict | None:
        for ev in ri.output_events:
            if ev.get("seq") == target_seq:
                if ev.get("kind") != "prompt" or ev.get("source") != "user":
                    return {"ok": False, "error": "target event is not a user prompt"}
                return ev
        return {"ok": False, "error": f"event seq={target_seq} not found"}

    def _get_jsonl_path(self, ri: RunInfo) -> str | dict:
        cwd = ri.workspace_path or os.getcwd()
        path = self._backend.get_session_path(ri.session_id, cwd)
        if not path:
            return {"ok": False, "error": f"session jsonl not found for session_id={ri.session_id[:8]}"}
        return path

    def _truncate_jsonl(self, ri: RunInfo, target_ev: dict, jsonl_path: str) -> dict | None:
        """截断 JSONL 文件到 target event 之前。返回 {"jsonl_cut_line", "backup"} 或错误 dict。"""
        target_text = target_ev.get("text", "")
        target_ts_ms = None
        try:
            target_ts_ms = int(datetime.fromisoformat(target_ev["ts"]).timestamp() * 1000)
        except Exception:
            pass

        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            return {"ok": False, "error": f"read jsonl failed: {e}"}

        cut_line_idx = self._find_cut_line(lines, target_text, target_ts_ms)
        if cut_line_idx is None:
            return {"ok": False, "error": "could not locate target prompt in jsonl (text/timestamp mismatch)"}

        backup = jsonl_path + f".rewind-{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        try:
            with open(backup, "w", encoding="utf-8") as f:
                f.writelines(lines)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.writelines(lines[:cut_line_idx])
            logger.info(f"[{ri.run_id[:8]}] rewind: jsonl truncated at line {cut_line_idx}, backup={os.path.basename(backup)}")
        except Exception as e:
            return {"ok": False, "error": f"truncate jsonl failed: {e}"}
        return {"jsonl_cut_line": cut_line_idx, "backup": backup}

    @staticmethod
    def _find_cut_line(lines: list[str], target_text: str, target_ts_ms: int | None) -> int | None:
        cut = None
        for idx, raw in enumerate(lines):
            try:
                obj = _json.loads(raw)
            except Exception:
                continue
            if obj.get("role") != "user" or obj.get("type") != "message":
                continue
            content = obj.get("content") or []
            if not content or not isinstance(content, list):
                continue
            first = content[0]
            if not isinstance(first, dict) or first.get("type") != "input_text":
                continue
            if first.get("text", "") != target_text:
                continue
            if target_ts_ms is not None:
                ts = obj.get("timestamp")
                if isinstance(ts, (int, float)) and abs(ts - target_ts_ms) > 60_000:
                    continue
            cut = idx  # 持续覆盖，命中多条取最后一条
        return cut

    def _truncate_memory(self, ri: RunInfo, target_seq: int, target_ev: dict) -> None:
        """截断内存中的事件、turn_markers、状态。"""
        kept = [e for e in ri.output_events if e.get("seq", 0) < target_seq]
        ri.output_events.clear()
        for e in kept:
            ri.output_events.append(e)
        ri._event_seq = max((e.get("seq", 0) for e in kept), default=0)

        target_text = target_ev.get("text", "")
        new_markers = []
        for m in ri.turn_markers:
            if isinstance(m, (list, tuple)) and len(m) >= 2 and m[1] == target_text:
                break
            new_markers.append(m)
        ri.turn_markers = new_markers

        ri.reported_result = None
        ri._fallback_result = None
        ri.user_terminated = False
        ri.exit_code = None
        ri.completed_at = None

    # ---- clear_context ----

    def clear_context(self, run_info: RunInfo) -> dict:
        """清空对话上下文（类似 /clear）。"""
        result = self._validate_clear(run_info)
        if result:
            return result

        jsonl_path = self._get_jsonl_path(run_info)
        if isinstance(jsonl_path, dict):
            return jsonl_path

        backup = self._clear_jsonl(run_info, jsonl_path)
        if isinstance(backup, dict):
            return backup

        self._clear_memory(run_info)
        run_info.add_event("system", text="Context cleared — ready for new input")
        return {"ok": True, "backup": backup}

    def _validate_clear(self, ri: RunInfo) -> dict | None:
        if ri.status in (RunStatus.RUNNING, RunStatus.WAITING):
            return {"ok": False, "error": f"cannot clear while status={ri.status.value}; stop the run first"}
        if not ri.session_id:
            return {"ok": False, "error": "run has no session_id"}
        return None

    def _clear_jsonl(self, ri: RunInfo, jsonl_path: str) -> str | dict:
        backup = jsonl_path + f".clear-{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        try:
            shutil.copy2(jsonl_path, backup)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.write("")
            logger.info(f"[{ri.run_id[:8]}] clear_context: jsonl cleared, backup={os.path.basename(backup)}")
        except Exception as e:
            return {"ok": False, "error": f"clear jsonl failed: {e}"}
        return backup

    @staticmethod
    def _clear_memory(ri: RunInfo) -> None:
        ri.output_events.clear()
        ri.turn_markers.clear()
        ri.messages.clear()
        ri._event_seq = 0
        ri.reported_result = None
        ri._fallback_result = None
        ri.user_terminated = False
        ri.exit_code = None
        ri.completed_at = None
