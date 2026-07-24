"""测试 CodeBuddy SDK 后端 — 验证消息转换和基本调用。"""
import sys
import os
import json
import queue
import threading
import time

# 把 src 加入 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent_os.src.agent.backend import CodeBuddySDKBackend, SessionHandle, _QueueLineReader


def test_sdk_stream():
    """测试 SDK 后端的 stream() 事件推送。"""
    backend = CodeBuddySDKBackend()

    # 用 launch + stream 测试真实调用
    handle = backend.launch(
        prompt="Say 'hello' and nothing else.",
        model="",
        system_prompt="Be concise.",
        cwd=os.getcwd(),
    )

    events = list(backend.stream(handle))
    kinds = [e.get("kind") for e in events]
    texts = [e.get("text", "") for e in events if e.get("kind") in ("text", "text_delta")]

    print(f"  Events: {len(events)}, kinds: {kinds}")
    print(f"  Texts: {texts}")
    assert any("hello" in t.lower() for t in texts), f"No 'hello' in {texts}"
    print("[PASS] SDK stream() test")


def test_queue_reader():
    """测试 QueueLineReader。"""
    q = queue.Queue()
    stop = threading.Event()
    reader = _QueueLineReader(q, stop)

    # 模拟写入
    def writer():
        q.put('line1\n')
        q.put('line2\n')
        time.sleep(0.1)
        stop.set()
        q.put(None)

    t = threading.Thread(target=writer)
    t.start()

    lines = []
    for line in iter(reader.readline, ""):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    t.join()

    assert len(lines) == 2
    assert lines[0] == "line1"
    assert lines[1] == "line2"
    print("[PASS] QueueLineReader")


def test_real_sdk_call():
    """真实 SDK 调用测试（需要已登录 codebuddy）。"""
    print("\n=== Real SDK call test ===")
    print("This test requires 'codebuddy' CLI to be authenticated.")

    backend = CodeBuddySDKBackend()

    handle = backend.launch(
        prompt="Say exactly 'hello from SDK' and nothing else.",
        model="",
        system_prompt="You are a concise assistant. Reply with exactly what is asked.",
        cwd=os.getcwd(),
    )

    print("Waiting for SDK response (timeout 60s)...")
    start = time.time()
    count = 0
    for ev in backend.stream(handle):
        elapsed = time.time() - start
        if elapsed > 60:
            print("[TIMEOUT]")
            handle.terminate()
            break
        count += 1
        kind = ev.get("kind", "?")
        if kind == "text":
            print(f"  [TEXT] {ev.get('text','')[:100]}")
        elif kind == "text_delta":
            print(ev.get("text", ""), end="", flush=True)
        elif kind == "result":
            print(f"\n  [RESULT] duration={ev.get('duration_ms',0)}ms")
        elif kind == "error":
            print(f"  [ERROR] {ev.get('error', '')}")
        else:
            print(f"  [{kind}]")

    handle.wait()
    print(f"\nTotal events: {count}")
    print("=== SDK call test done ===")


if __name__ == "__main__":
    test_queue_reader()

    import sys
    if "--real" in sys.argv:
        test_real_sdk_call()
    else:
        print("\nSkipped real SDK call test. Use --real to run it.")
