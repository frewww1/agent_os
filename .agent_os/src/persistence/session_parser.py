import json
from datetime import datetime, timezone

def _ts_from_obj(obj: dict) -> str:
    """从 jsonl 的 timestamp (ms) 转为 ISO 格式 ts 字段。"""
    ts_ms = obj.get("timestamp")
    if ts_ms:
        try:
            return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def parse_cli_session_jsonl(line: str) -> list[dict]:
    """解析 CodeBuddy CLI 会话 jsonl 行，转为前端事件格式。
    
    事件格式与 stream_parser 输出一致：
    - tool_use:  {"kind":"tool_use","tool":"Bash","summary":"ls"}
    - tool_result: {"kind":"tool_result","text":"...","truncated":true}
    - reasoning: {"kind":"thinking","text":"..."}
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    
    try:
        events = _parse_session_obj(obj)
        ts = _ts_from_obj(obj)
        for ev in events:
            ev.setdefault("ts", ts)
        return events
    except Exception:
        return []


def _parse_args(args_raw) -> dict:
    """解析 arguments，支持 JSON 字符串和 dict。"""
    if isinstance(args_raw, dict):
        return args_raw
    if isinstance(args_raw, str):
        try:
            return json.loads(args_raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _tool_summary(tool_name: str, inp: dict) -> str:
    """根据工具名提取有意义的摘要。"""
    if tool_name == "Bash":
        return inp.get("command", "") or json.dumps(inp, ensure_ascii=False)
    if tool_name in ("Read", "Write", "Edit"):
        return inp.get("file_path", "")
    if tool_name == "Grep":
        return inp.get("pattern", "")
    if tool_name == "Glob":
        return inp.get("pattern", "")
    if tool_name == "TodoWrite":
        todos = inp.get("todos", [])
        return f"{len(todos)} todo(s)" if isinstance(todos, list) else ""
    return json.dumps(inp, ensure_ascii=False)[:200]


def _parse_session_obj(obj: dict) -> list[dict]:
    typ = obj.get("type", "")
    events = []
    
    if typ == "message":
        role = obj.get("role", "")
        content = obj.get("content", [])
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                bt = block.get("type", "")
                if bt in ("input_text", "output_text"):
                    text_parts.append(block.get("text", ""))
        text = "".join(text_parts)
        if text:
            if role == "assistant":
                events.append({"kind": "text", "text": text, "role": "assistant",
                               "source": "assistant"})
            else:
                events.append({"kind": "prompt", "text": text, "role": "user",
                               "source": "user"})
    
    elif typ == "reasoning":
        raw_content = obj.get("rawContent", [])
        text = ""
        for block in raw_content:
            if isinstance(block, dict) and block.get("type") == "reasoning_text":
                text = block.get("text", "")
                break
        if not text:
            text = obj.get("text", "")
        if text:
            events.append({"kind": "thinking", "text": text})
    
    elif typ == "function_call":
        name = obj.get("name", "")
        inp = _parse_args(obj.get("arguments", ""))
        events.append({"kind": "tool_use", "tool": name,
                       "summary": _tool_summary(name, inp)})
    
    elif typ == "function_call_result":
        output = obj.get("output", "")
        # CodeBuddy: output 是 {"type":"text","text":"..."} 对象
        if isinstance(output, dict):
            output_text = output.get("text", "") or json.dumps(output, ensure_ascii=False)
        else:
            output_text = str(output) if output else ""
        truncated = len(output_text) > 800
        if truncated:
            output_text = output_text[:800] + "\n... (truncated)"
        events.append({"kind": "tool_result", "text": output_text, "truncated": truncated})
    
    elif typ == "file-history-snapshot":
        pass
    
    elif typ == "error":
        events.append({"kind": "error", "text": obj.get("message", "")})
    
    return events
