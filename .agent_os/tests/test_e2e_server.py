"""端到端测试：启动服务 → 创建 run → 流式 SSE → 验证输出。

测试两种后端：
    python .agent_os/tests/test_e2e_server.py --backend codebuddy-sdk
    python .agent_os/tests/test_e2e_server.py --backend native
"""
import sys, os, json, time, threading, argparse
import urllib.request
import urllib.error

# 先启动服务
import subprocess
import signal

BACKEND = "codebuddy-sdk"
PORT = 18420  # 避免和默认 8420 冲突

def start_server():
    env = os.environ.copy()
    env["AGENT_OS_BACKEND"] = BACKEND
    proc = subprocess.Popen(
        [sys.executable, ".agent_os/main.py", "--port", str(PORT), "--backend", BACKEND, "--no-browser"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    # 等待服务就绪
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/models", timeout=2)
            print("[OK] Server started")
            return proc
        except Exception:
            time.sleep(1)
    proc.kill()
    raise RuntimeError("Server failed to start")

def api(path, data=None, method="GET"):
    url = f"http://127.0.0.1:{PORT}{path}"
    if data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(), method=method,
            headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        raise RuntimeError(f"HTTP {e.code}: {body}")

def test_models():
    print("--- test_models ---")
    data = api("/api/models")
    models = data.get("models", [])
    print(f"  Models: {models}")
    assert len(models) > 0, "No models"
    print("  [PASS]")

def test_create_and_stream():
    print("--- test_create_and_stream ---")
    # 创建 run
    result = api("/api/run", data={
        "prompt": "Reply with exactly 'E2E_OK' and nothing else.",
        "model": "claude-sonnet-4.6" if "codebuddy" in BACKEND else None,
    }, method="POST")
    run_id = result.get("run_id")
    assert run_id, f"No run_id in {result}"
    print(f"  Run ID: {run_id}")

    # SSE 流式读取
    url = f"http://127.0.0.1:{PORT}/api/run/{run_id}/stream"
    req = urllib.request.Request(url)
    events = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        buffer = ""
        while True:
            chunk = resp.read(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                line, buffer = buffer.split("\n\n", 1)
                if line.startswith("data: "):
                    data_str = line[6:]
                    try:
                        ev = json.loads(data_str)
                        events.append(ev)
                    except json.JSONDecodeError:
                        pass
                elif line.startswith("event: done"):
                    break

    print(f"  Events: {len(events)}")
    kinds = [e.get("kind") for e in events]
    print(f"  Kinds: {set(kinds)}")

    # 验证
    all_text = " ".join(e.get("text", "") for e in events if e.get("kind") in ("text", "text_delta"))
    assert "E2E_OK" in all_text, f"No E2E_OK in output: {all_text[:100]}"
    print("  [PASS]")

    # 查 run 状态
    run_data = api(f"/api/run/{run_id}")
    assert run_data["status"] == "completed", f"Status: {run_data['status']}"
    print(f"  Status: {run_data['status']}, exit_code: {run_data.get('exit_code')}")
    print("  [PASS]")

def test_continue():
    print("--- test_continue ---")
    # 创建 run
    result = api("/api/run", data={
        "prompt": "Remember this number: 99. Reply 'OK, remembered 99.'",
        "model": "claude-sonnet-4.6" if "codebuddy" in BACKEND else None,
    }, method="POST")
    run_id = result["run_id"]

    # 等完成
    for _ in range(60):
        run_data = api(f"/api/run/{run_id}")
        if run_data["status"] != "running":
            break
        time.sleep(1)
    assert run_data["status"] == "completed"

    # continue
    api(f"/api/run/{run_id}/continue", data={
        "prompt": "What number did I ask you to remember? Reply with just the number.",
    }, method="POST")

    # 等完成
    for _ in range(60):
        run_data = api(f"/api/run/{run_id}")
        if run_data["status"] != "running":
            break
        time.sleep(1)

    events = run_data.get("events", [])
    all_text = " ".join(e.get("text", "") for e in events if e.get("kind") in ("text", "text_delta"))
    print(f"  Resume output: {all_text[:100]}")
    assert "99" in all_text, f"Agent didn't remember 99: {all_text[:100]}"
    print("  [PASS]")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="codebuddy-sdk")
    args = parser.parse_args()
    BACKEND = args.backend

    print(f"=== E2E Server Test (backend={BACKEND}) ===")
    server = start_server()
    try:
        test_models()
        test_create_and_stream()
        test_continue()
        print("\n=== ALL PASSED ===")
    finally:
        server.terminate()
        server.wait(timeout=5)
        print("Server stopped")
