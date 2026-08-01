"""测试 SDK 多轮对话（session resume）。"""
import sys, os, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_os.src.agent import CodeBuddySDKBackend, SDKHandle


def run_sdk(prompt, resume_session=None, session_id=None, system_prompt=None):
    """运行一次 SDK 调用，返回 (events, session_id)。"""
    backend = CodeBuddySDKBackend()
    handle = SDKHandle(session_id=session_id or "")

    def _run():
        try:
            backend._call_sdk(
                handle=handle,
                prompt=prompt, model="", session_id=session_id,
                resume_session=resume_session, system_prompt=system_prompt,
                cwd=os.getcwd(), env=None, stop=handle._stop,
            )
        except Exception as e:
            backend._emit_event(handle, "error", error=str(e))
        finally:
            handle._stop.set()
            handle._events_queue.put(None)

    t = threading.Thread(target=_run, daemon=True)
    handle._thread = t
    t.start()

    events = []
    extracted_session = None
    for ev in backend.stream(handle):
        events.append(ev)
        if ev.get("kind") == "result":
            extracted_session = ev.get("session_id", "")
        elif ev.get("kind") == "system":
            extracted_session = ev.get("session_id", "") or extracted_session
    t.join(timeout=5)
    return events, extracted_session


def test_session_resume():
    print("=== Session Resume Test ===")

    # Round 1: 让 agent 记住一个数字
    print("\n[Round 1] Asking to remember a number...")
    lines1, session_id = run_sdk(
        prompt="Remember this number: 42. Just reply 'OK, remembered 42.'",
        system_prompt="You are concise. Reply exactly as requested.",
    )
    assert session_id, "No session_id from round 1"
    print(f"  Session ID: {session_id[:13]}...")
    for l in lines1:
        obj = json.loads(l)
        if obj.get("type") == "assistant":
            for b in obj.get("message", {}).get("content", []):
                if b.get("type") == "text":
                    print(f"  [Agent] {b['text'][:100]}")

    # Round 2: resume 同一个 session，问它记住的数字
    print(f"\n[Round 2] Resuming session {session_id[:13]}..., asking what number was remembered")
    lines2, _ = run_sdk(
        prompt="What number did I ask you to remember earlier? Reply with just the number.",
        resume_session=session_id,
    )
    for l in lines2:
        obj = json.loads(l)
        if obj.get("type") == "assistant":
            for b in obj.get("message", {}).get("content", []):
                if b.get("type") == "text":
                    print(f"  [Agent] {b['text'][:100]}")

    # 检查是否提到 42
    all_text = ""
    for l in lines2:
        try:
            obj = json.loads(l)
            if obj.get("type") == "assistant":
                for b in obj.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        all_text += b["text"]
        except json.JSONDecodeError:
            pass
    if "42" in all_text:
        print("\n[PASS] Session resume works! Agent remembered 42.")
    else:
        print(f"\n[WARN] Agent may not have remembered 42. Response: {all_text[:200]}")


if __name__ == "__main__":
    test_session_resume()
