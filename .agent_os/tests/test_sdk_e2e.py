"""端到端测试：SDK backend → stream → 事件流。"""
import sys, os, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_os.src.agent import CodeBuddySDKBackend, SDKHandle


def test_e2e():
    print("=== End-to-end: SDK → stream → events ===\n")

    backend = CodeBuddySDKBackend()
    handle = SDKHandle()

    def _run():
        try:
            backend._call_sdk(
                handle=handle,
                prompt="Say exactly 'hello from SDK' and nothing else.",
                model="", session_id=None, resume_session=None,
                system_prompt="You are a concise assistant.",
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

    count = 0
    for ev in backend.stream(handle):
        count += 1
        kind = ev.get("kind", "?")
        if kind == "text":
            print(f"  [TEXT] {ev.get('text','')[:100]}")
        elif kind == "text_delta":
            print(ev.get("text", ""), end="", flush=True)
        elif kind == "result":
            print(f"\n  [RESULT] duration={ev.get('duration_ms',0)}ms")
        else:
            print(f"  [{kind}] {str(ev)[:120]}")

    print(f"\nTotal events: {count}")
    print("=== SDK call test done ===")


if __name__ == "__main__":
    test_e2e()
