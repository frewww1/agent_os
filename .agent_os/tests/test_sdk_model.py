"""测试 SDK 指定模型调用。"""
import sys, os, json, queue, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from backend import CodeBuddySDKBackend, FakeProcess


def test_with_model(model_id: str):
    print(f"\n=== Test with model: {model_id} ===")

    backend = CodeBuddySDKBackend()
    q = queue.Queue()
    stop = threading.Event()
    backend._output_queue = q
    backend._stop_event = stop
    fp = FakeProcess(q, stop)

    def _run():
        try:
            backend._call_sdk(
                prompt="Say exactly 'model test OK' and nothing else.",
                model=model_id,
                session_id=None, resume_session=None,
                system_prompt="You are concise. Reply exactly as instructed.",
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

    start = time.time()
    result_text = ""
    used_model = "?"
    for line in iter(fp.stdout.readline, ""):
        if time.time() - start > 30:
            print("  [TIMEOUT]")
            stop.set()
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            msg_type = obj.get("type", "")
            if msg_type == "assistant":
                for block in obj.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        result_text += block["text"]
                        print(f"  [TEXT] {block['text'][:100]}")
                used_model = obj.get("message", {}).get("model", "?")
            elif msg_type == "stream_event":
                delta = obj.get("event", {}).get("delta", {})
                if delta.get("type") == "text_delta":
                    print(delta.get("text", ""), end="", flush=True)
            elif msg_type == "result":
                print(f"\n  [DONE] {obj.get('duration_ms', 0)}ms, model={used_model}")
            elif msg_type == "error":
                print(f"  [ERROR] {obj.get('error', '')[:200]}")
        except json.JSONDecodeError:
            pass

    t.join(timeout=5)
    elapsed = time.time() - start
    print(f"  Elapsed: {elapsed:.1f}s")
    return result_text, used_model


if __name__ == "__main__":
    # 先列出可用模型
    backend = CodeBuddySDKBackend()
    models = backend.list_models(['codebuddy'])
    print(f"Available models: {models}")

    # 用第一个模型测试
    if models:
        text, model = test_with_model(models[0])
        if "model test OK" in text.lower():
            print(f"\n[PASS] Model {model} works!")
        else:
            print(f"\n[WARN] Unexpected response from {model}: {text[:100]}")
    else:
        print("No models available, skipping test.")
