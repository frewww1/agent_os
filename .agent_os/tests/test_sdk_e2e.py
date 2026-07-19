"""端到端测试：SDK backend → stream_parser 解析 → 事件流。"""
import sys, os, json, queue, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 直接内联 stream_parser 的解析逻辑，避免相对导入问题
def parse_stream_json_events(line: str) -> list[dict]:
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        stripped = line.strip()
        if not stripped:
            return []
        return [{"kind": "raw", "text": line}]

    msg_type = obj.get("type", "")
    events = []

    if msg_type == "stream_event":
        inner = obj.get("event", {})
        inner_type = inner.get("type", "")
        if inner_type in ("content_block_start", "content_block_delta"):
            delta = inner.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    events.append({"kind": "text_delta", "text": text})
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
                    events.append({
                        "kind": "tool_use",
                        "tool": block.get("name", "?"),
                        "summary": json.dumps(block.get("input", {}), ensure_ascii=False)[:200],
                    })
        return events

    if msg_type == "user":
        message = obj.get("message", {})
        content = message.get("content", [])
        for block in (content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            text = block.get("content", "")
            if isinstance(text, list):
                text = "\n".join(
                    rc.get("text", "") for rc in text
                    if isinstance(rc, dict) and rc.get("type") == "text"
                )
            if text:
                truncated = len(text) > 800
                events.append({
                    "kind": "tool_result",
                    "text": text[:800] + ("\n... (truncated)" if truncated else ""),
                    "truncated": truncated,
                })
        return events

    return []


def extract_session_id(line: str) -> str | None:
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    sid = obj.get("session_id", "")
    if sid:
        return sid
    data = obj.get("data", {})
    if isinstance(data, dict):
        return data.get("session_id", "")
    return None


from backend import CodeBuddySDKBackend, FakeProcess


def test_e2e():
    print("=== End-to-end: SDK → stream_parser → events ===\n")

    backend = CodeBuddySDKBackend()
    q = queue.Queue()
    stop = threading.Event()
    backend._output_queue = q
    backend._stop_event = stop
    fp = FakeProcess(q, stop)

    def _run():
        try:
            backend._call_sdk(
                prompt="Say 'hello world'.",
                model="",
                session_id=None, resume_session=None,
                system_prompt="You are concise. Just reply as instructed.",
                mcp_config=None, cwd=os.getcwd(), env=None, stop=stop,
            )
        except Exception as e:
            q.put(json.dumps({"type": "error", "error": str(e)}) + "\n")
        finally:
            stop.set()
            fp._returncode = 0
            q.put(None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    session_id = None
    events_by_kind = {}
    line_count = 0

    for line in iter(fp.stdout.readline, ""):
        stripped = line.rstrip("\n\r")
        if not stripped:
            continue
        line_count += 1

        sid = extract_session_id(stripped)
        if sid:
            session_id = sid

        events = parse_stream_json_events(stripped)
        for ev in events:
            kind = ev.get("kind", "raw")
            events_by_kind.setdefault(kind, 0)
            events_by_kind[kind] += 1

            if kind == "text":
                print(f"  [text] {ev.get('text','')[:80]}")
            elif kind == "tool_use":
                print(f"  [tool] {ev.get('tool','?')}: {ev.get('summary','')[:60]}")
            elif kind == "tool_result":
                print(f"  [tool_result] {ev.get('text','')[:60]}")

    t.join(timeout=5)
    print(f"\nTotal lines: {line_count}")
    print(f"Session ID: {session_id}")
    print(f"Events by kind: {json.dumps(events_by_kind, indent=2)}")

    assert events_by_kind.get("text", 0) > 0, "No text events"
    print("\n[PASS] End-to-end test passed!")


if __name__ == "__main__":
    test_e2e()
