"""端到端全功能测试 — 启动服务，覆盖所有核心 API。

    python .agent_os/tests/test_e2e_full.py --backend codebuddy-sdk
    python .agent_os/tests/test_e2e_full.py --backend native
"""
import sys, os, json, time, threading, argparse, subprocess

BACKEND = "codebuddy-sdk"
PORT = 19521
BASE = f"http://127.0.0.1:{PORT}"

# ============================================================================
# HTTP 工具
# ============================================================================
import urllib.request, urllib.error

def api(path, data=None, method="GET", timeout=10):
    url = f"{BASE}{path}"
    if data is not None:
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, method=method,
            headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {body}")

def sse_stream(run_id, timeout=120):
    """读取 SSE 流，返回 events 列表。"""
    url = f"{BASE}/api/run/{run_id}/stream"
    req = urllib.request.Request(url)
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = ""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buf:
                line, buf = buf.split("\n\n", 1)
                if line.startswith("data: "):
                    try:
                        events.append(json.loads(line[6:]))
                    except json.JSONDecodeError:
                        pass
                elif line.startswith("event: done"):
                    return events
    return events

def wait_for_status(run_id, target="completed", timeout=120):
    for _ in range(timeout):
        try:
            r = api(f"/api/run/{run_id}")
            if r["status"] == target:
                return r
            if r["status"] in ("failed", "stopped") and target not in ("failed", "stopped"):
                raise RuntimeError(f"Run {target} but got {r['status']}")
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError(f"Run {run_id} did not reach {target} in {timeout}s")

# ============================================================================
# 服务管理
# ============================================================================
def start_server():
    env = os.environ.copy()
    env["AGENT_OS_BACKEND"] = BACKEND
    proc = subprocess.Popen(
        [sys.executable, ".agent_os/main.py", "--port", str(PORT),
         "--backend", BACKEND, "--no-browser"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    # 后台线程读 stdout/stderr 防止管道阻塞
    server_log = []
    def _read(pipe, label):
        try:
            for line in iter(pipe.readline, b""):
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    server_log.append(f"[{label}] {decoded}")
        except Exception:
            pass
    threading.Thread(target=_read, args=(proc.stdout, "out"), daemon=True).start()
    threading.Thread(target=_read, args=(proc.stderr, "err"), daemon=True).start()

    for _ in range(30):
        try:
            urllib.request.urlopen(f"{BASE}/api/models", timeout=2)
            print("[OK] Server started")
            return proc
        except Exception:
            time.sleep(1)
    proc.kill()
    # 打印日志帮助诊断
    print("Server log (last 20 lines):")
    for line in server_log[-20:]:
        print(f"  {line[:120]}")
    raise RuntimeError("Server failed to start")

# ============================================================================
# 测试用例
# ============================================================================
passed = 0
failed = 0

def test(name, fn):
    global passed, failed
    print(f"\n{'='*50}")
    print(f"  {name}")
    print(f"{'='*50}")
    try:
        fn()
        passed += 1
        print(f"  [PASS] {name}")
    except Exception as e:
        failed += 1
        print(f"  [FAIL] {name}: {e}")
        import traceback
        traceback.print_exc()

# ---------------------------------------------------------------------------
def test_models():
    r = api("/api/models")
    models = r.get("models", [])
    assert len(models) > 0, "No models"
    print(f"  Models ({len(models)}): {models[:5]}...")

def test_create_run():
    r = api("/api/run", method="POST", data={
        "prompt": "Reply 'OK1' and nothing else.",
        "model": "claude-sonnet-4.6" if "codebuddy" in BACKEND else None,
    })
    run_id = r["run_id"]
    assert run_id, "No run_id"
    print(f"  Run ID: {run_id}")
    return run_id

def test_sse_stream(run_id):
    events = sse_stream(run_id)
    kinds = set(e.get("kind") for e in events)
    all_text = " ".join(e.get("text","") for e in events if e.get("kind") in ("text","text_delta"))
    print(f"  Events: {len(events)}, kinds: {kinds}")
    print(f"  Output: {all_text[:80]}")
    assert "OK1" in all_text, f"No OK1 in: {all_text[:80]}"

def test_get_run(run_id):
    r = api(f"/api/run/{run_id}")
    assert r["status"] == "completed", f"Status: {r['status']}"
    assert len(r.get("events", [])) > 0, "No events"
    print(f"  Status: {r['status']}, events: {len(r['events'])}")

def test_continue_run(run_id):
    api(f"/api/run/{run_id}/continue", method="POST", data={
        "prompt": "What did you just say? Reply with just the word you said.",
    })
    r = wait_for_status(run_id, "completed")
    events = r.get("events", [])
    all_text = " ".join(e.get("text","") for e in events if e.get("kind") in ("text","text_delta"))
    assert "OK1" in all_text, f"Agent forgot context: {all_text[:80]}"
    print(f"  Resume OK, output contains OK1")

def test_stop_run():
    r = api("/api/run", method="POST", data={
        "prompt": "Write a Python script that prints numbers 1 to 1000.",
        "model": "claude-sonnet-4.6" if "codebuddy" in BACKEND else None,
    })
    run_id = r["run_id"]
    time.sleep(3)
    api(f"/api/run/{run_id}/stop", method="POST")
    r = api(f"/api/run/{run_id}")
    assert r["status"] == "stopped", f"Expected stopped, got {r['status']}"
    print(f"  Stopped OK")

def test_rewind(run_id):
    """回退到第一轮对话之前。"""
    r = api(f"/api/run/{run_id}")
    events = r.get("events", [])
    # 找第一个 user prompt
    target_seq = None
    for ev in events:
        if ev.get("kind") == "prompt" and ev.get("source") == "user":
            target_seq = ev.get("seq")
            break
    if not target_seq:
        print("  No user prompt found, skipping rewind")
        return

    api(f"/api/run/{run_id}/rewind", method="POST", data={"seq": target_seq})
    r = api(f"/api/run/{run_id}")
    assert r["status"] == "stopped", f"After rewind status: {r['status']}"
    events_after = r.get("events", [])
    assert len(events_after) < len(events), f"Events not truncated: {len(events_after)} >= {len(events)}"
    print(f"  Rewind OK, events: {len(events)} → {len(events_after)}")

def test_clear_context(run_id):
    api(f"/api/run/{run_id}/clear", method="POST")
    r = api(f"/api/run/{run_id}")
    assert r["status"] == "stopped"
    print(f"  Clear OK")

def test_export(run_id):
    r = api(f"/api/run/{run_id}/export?format=md")
    assert isinstance(r, dict) or isinstance(r, str), f"Export type: {type(r)}"
    print(f"  Export OK")

def test_list_runs():
    r = api("/api/runs")
    assert isinstance(r, list), f"Expected list, got {type(r)}"
    print(f"  Runs: {len(r)}")

def test_tree():
    r = api("/api/tree")
    assert isinstance(r, list), f"Expected list"
    print(f"  Tree roots: {len(r)}")

def test_workspaces():
    r = api("/api/workspaces")
    assert isinstance(r, list)
    print(f"  Workspaces: {len(r)}")

def test_completions():
    r = api("/api/completions")
    assert isinstance(r, dict)
    print(f"  Completions keys: {list(r.keys())}")

def test_label(run_id):
    api(f"/api/run/{run_id}/label", method="POST", data={"label": "e2e-test"})
    r = api(f"/api/run/{run_id}")
    assert r.get("label") == "e2e-test", f"Label: {r.get('label')}"
    print(f"  Label OK: {r['label']}")

def test_delete(run_id):
    api(f"/api/run/{run_id}", method="DELETE")
    try:
        api(f"/api/run/{run_id}")
        assert False, "Should have been deleted"
    except RuntimeError:
        pass
    print(f"  Delete OK")

def test_dag_templates():
    r = api("/api/dag/templates")
    assert isinstance(r, list)
    print(f"  DAG templates: {len(r)}")

def test_set_goal(run_id):
    api(f"/api/run/{run_id}/set-goal", method="POST", data={"goal": "Reply with OK"})
    r = api(f"/api/run/{run_id}")
    assert r.get("goal") == "Reply with OK"
    print(f"  Goal set: {r['goal']}")

# ---------------------------------------------------------------------------
def main():
    global BACKEND
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="codebuddy-sdk")
    args = parser.parse_args()
    BACKEND = args.backend

    print(f"\n{'#'*60}")
    print(f"  Agent OS E2E Full Test (backend={BACKEND})")
    print(f"{'#'*60}")

    server = start_server()
    try:
        # 基础
        test("GET /api/models", test_models)
        test("GET /api/runs", test_list_runs)
        test("GET /api/tree", test_tree)
        test("GET /api/workspaces", test_workspaces)
        test("GET /api/completions", test_completions)
        test("GET /api/dag/templates", test_dag_templates)

        # Run 生命周期
        run_id = test("POST /api/run (create)", test_create_run)
        test("SSE /api/run/{id}/stream", lambda: test_sse_stream(run_id))
        test("GET /api/run/{id}", lambda: test_get_run(run_id))
        test("POST /api/run/{id}/set-goal", lambda: test_set_goal(run_id))
        test("POST /api/run/{id}/continue", lambda: test_continue_run(run_id))
        test("POST /api/run/{id}/label", lambda: test_label(run_id))
        test("POST /api/run/{id}/rewind", lambda: test_rewind(run_id))
        test("POST /api/run/{id}/clear", lambda: test_clear_context(run_id))
        test("GET /api/run/{id}/export", lambda: test_export(run_id))
        test("DELETE /api/run/{id}", lambda: test_delete(run_id))

        # 停止
        test("POST /api/run/{id}/stop", test_stop_run)

    finally:
        server.terminate()
        server.wait(timeout=5)

    print(f"\n{'#'*60}")
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print(f"{'#'*60}")
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
