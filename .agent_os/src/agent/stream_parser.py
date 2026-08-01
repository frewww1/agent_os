"""Agent OS Stream-JSON 解析器：将 CLI 的 stream-json 输出行转为结构化事件。

工具解析采用注册表模式（_TOOL_HANDLERS），新增 CLI 后端时只需注册新的工具处理函数，
无需修改核心解析逻辑。
"""
import json as _json
import logging
from typing import Any, Callable

from ..utils import sanitize

logger = logging.getLogger("agent_os")

# ============================================================================
# 工具解析注册表
# ============================================================================

ToolHandler = Callable[[dict, dict], dict]
"""工具处理函数签名：(block, tool_input) -> event_dict"""

_TOOL_HANDLERS: dict[str, ToolHandler] = {}
"""注册的工具处理函数，key 为工具名（如 'Bash', 'Write'）。"""


def register_tool(name: str) -> Callable[[ToolHandler], ToolHandler]:
    """装饰器：注册工具处理函数。"""
    def decorator(fn: ToolHandler) -> ToolHandler:
        _TOOL_HANDLERS[name] = fn
        return fn
    return decorator


def handle_tool(block: dict) -> dict:
    """根据 block['name'] 分发到注册的处理函数，返回 event_dict。"""
    tool_name = block.get("name", "?")
    tool_input = block.get("input", {}) or {}
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler:
        return handler(block, tool_input)
    # 未注册的工具：通用处理
    return {
        "kind": "tool_use",
        "tool": tool_name,
        "summary": _json.dumps(tool_input, ensure_ascii=False)[:200],
    }


# ---- 注册内置工具处理函数 ----

@register_tool("Bash")
def _handle_bash(block: dict, inp: dict) -> dict:
    return {"kind": "tool_use", "tool": "Bash", "summary": inp.get("command", "")}


@register_tool("Write")
def _handle_write(block: dict, inp: dict) -> dict:
    return {
        "kind": "tool_use",
        "tool": "Write",
        "summary": inp.get("file_path", ""),
        "file_path": inp.get("file_path", ""),
        "content": inp.get("content", ""),
    }


@register_tool("Edit")
def _handle_edit(block: dict, inp: dict) -> dict:
    return {
        "kind": "tool_use",
        "tool": "Edit",
        "summary": inp.get("file_path", ""),
        "file_path": inp.get("file_path", ""),
        "old_string": inp.get("old_string", ""),
        "new_string": inp.get("new_string", ""),
    }


@register_tool("Read")
def _handle_read(block: dict, inp: dict) -> dict:
    return {"kind": "tool_use", "tool": "Read", "summary": inp.get("file_path", "")}


@register_tool("Grep")
def _handle_grep(block: dict, inp: dict) -> dict:
    return {"kind": "tool_use", "tool": "Grep", "summary": inp.get("pattern", "")}


@register_tool("Glob")
def _handle_glob(block: dict, inp: dict) -> dict:
    return {"kind": "tool_use", "tool": "Glob", "summary": inp.get("pattern", "")}


@register_tool("TodoWrite")
def _handle_todo_write(block: dict, inp: dict) -> dict:
    todos = inp.get("todos", [])
    event = {
        "kind": "tool_use",
        "tool": "TodoWrite",
        "summary": f"{len(todos)} todo(s)",
    }
    if isinstance(todos, list):
        event["todos"] = todos
    return event


@register_tool("ExitPlanMode")
def _handle_exit_plan_mode(block: dict, inp: dict) -> dict:
    allowed_prompts = inp.get("allowedPrompts", [])
    return {
        "kind": "plan_pending",
        "tool": "ExitPlanMode",
        "summary": f"Plan ready — awaiting approval ({len(allowed_prompts)} allowed actions)",
        "allowed_prompts": allowed_prompts,
    }


# ============================================================================
# 公共 API
# ============================================================================

def parse_stream_json_events(line: str) -> list[dict]:
    """解析 stream-json 一行，返回结构化事件列表。

    支持 message types:
    - assistant: 文本 + 工具调用（通过注册表分发）
    - user: 工具结果回传（截断 >800 字符）
    - system/result: 忽略
    """
    try:
        obj = _json.loads(line)
        obj = sanitize(obj)
    except (ValueError, TypeError):
        stripped = line.strip()
        if not stripped:
            return []
        return [{"kind": "raw", "text": line}]

    msg_type = obj.get("type", "")
    events: list[dict] = []

    # 逐 token 流式事件（--include-partial-messages）
    if msg_type == "stream_event":
        inner = obj.get("event", {})
        inner_type = inner.get("type", "")
        if inner_type in ("content_block_start", "content_block_delta"):
            delta = inner.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    events.append({"kind": "text_delta", "text": text})
            elif delta_type == "thinking_delta":
                text = delta.get("thinking", "")
                if text:
                    events.append({"kind": "thinking_delta", "text": text})
        return events

    if msg_type == "assistant":
        message = obj.get("message", {})
        content = message.get("content", [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                if bt == "text":
                    text = block.get("text", "")
                    if text:
                        events.append({"kind": "text", "text": text})
                elif bt == "tool_use":
                    events.append(handle_tool(block))
        return events

    if msg_type == "user":
        events.extend(_parse_user_events(obj))
        return events

    return []


def _parse_user_events(obj: dict) -> list[dict]:
    """解析 user 消息中的工具结果。"""
    events: list[dict] = []
    message = obj.get("message", {})
    content = message.get("content", [])
    if not isinstance(content, list):
        return events
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        text = _extract_result_text(block.get("content", ""))
        if not text:
            continue
        truncated = len(text) > 800
        if truncated:
            text = text[:800] + "\n... (truncated)"
        events.append({
            "kind": "tool_result",
            "text": text,
            "truncated": truncated,
        })
    return events


def _extract_result_text(content: Any) -> str:
    """从 tool_result content 中提取文本。支持 str 和 list[dict] 两种格式。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for rc in content:
            if isinstance(rc, dict) and rc.get("type") == "text":
                parts.append(rc.get("text", ""))
        return "\n".join(parts)
    return ""
