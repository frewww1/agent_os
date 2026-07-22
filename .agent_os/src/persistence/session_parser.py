import json

def parse_cli_session_jsonl(line: str) -> list[dict]:
    """解析 CodeBuddy CLI 会话 jsonl 行，转为前端事件格式。
    
    CodeBuddy session 格式 vs stream-json 格式完全不同：
    - session: {"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}
    - stream:  {"kind":"text","text":"..."}
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    
    try:
        return _parse_session_obj(obj)
    except Exception:
        return []


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
            events.append({"kind": "text" if role == "assistant" else "prompt", 
                           "text": text, "role": role})
    
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
            events.append({"kind": "text", "text": f"💭 {text}"})
    
    elif typ == "function_call":
        name = obj.get("name", "") or (obj.get("function", {}) or {}).get("name", "")
        args = obj.get("arguments", "") or (obj.get("function", {}) or {}).get("arguments", "")
        args_display = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        events.append({"kind": "tool_use", "text": f"{name}({args_display[:100]})",
                       "tool_name": name, "tool_input": args})
    
    elif typ == "function_call_result":
        output = obj.get("output", "")
        # CodeBuddy: output 是 {"type":"text","text":"..."} 对象
        if isinstance(output, dict):
            output_text = output.get("text", "") or json.dumps(output, ensure_ascii=False)
        else:
            output_text = str(output) if output else ""
        events.append({"kind": "tool_result", "text": output_text[:2000]})
    
    elif typ == "file-history-snapshot":
        pass
    
    elif typ == "error":
        events.append({"kind": "error", "text": obj.get("message", "")})
    
    return events
