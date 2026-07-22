import json, re

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
        text = obj.get("text", "") or obj.get("rawText", "")
        if text:
            events.append({"kind": "text", "text": f"💭 {text}"})
    
    elif typ == "function_call":
        name = obj.get("name", "") or (obj.get("function", {}) or {}).get("name", "")
        args = obj.get("arguments", "") or (obj.get("function", {}) or {}).get("arguments", "")
        events.append({"kind": "tool_use", "text": f"{name}({args[:100]})",
                       "tool_name": name, "tool_input": args})
    
    elif typ == "function_call_result":
        output = obj.get("output", "")
        events.append({"kind": "tool_result", "text": output[:500]})
    
    elif typ == "file-history-snapshot":
        pass  # 跳过文件快照
    
    elif typ == "error":
        events.append({"kind": "error", "text": obj.get("message", "")})
    
    return events
