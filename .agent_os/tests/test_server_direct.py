"""直接测试：不通过 subprocess，直接用 import 测试服务能否响应。"""
import sys, os, importlib.util, json, time, threading
from pathlib import Path

# 注册包
_this_dir = Path(__file__).parent.parent
for pkg, loc in [("agent_os", _this_dir), ("agent_os.src", _this_dir / "src")]:
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(pkg, loc / "__init__.py", submodule_search_locations=[str(loc)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)

os.environ["AGENT_OS_BACKEND"] = "codebuddy-sdk"

# 直接 import 验证
from agent_os.src.core.agent_os import AgentOS
from agent_os.dashboard.app import app, set_agent_os
import asyncio

agent_os = AgentOS(project_root=os.getcwd(), cli_command="codebuddy", port=19551)
set_agent_os(agent_os)
print("AgentOS OK, models:", agent_os.list_models())

# 测试 API（不启动 uvicorn，直接调 FastAPI TestClient）
from fastapi.testclient import TestClient

client = TestClient(app)

# 1. /api/models
resp = client.get("/api/models")
assert resp.status_code == 200, f"models: {resp.status_code}"
models = resp.json().get("models", [])
print(f"  [OK] /api/models: {len(models)} models")

# 2. POST /api/run
resp = client.post("/api/run", json={
    "prompt": "Reply 'E2E_OK' and nothing else.",
    "model": "claude-sonnet-4.6",
})
assert resp.status_code == 200, f"create run: {resp.status_code} {resp.text}"
run_id = resp.json()["run_id"]
print(f"  [OK] POST /api/run: {run_id}")

# 3. GET /api/run/{id} (poll until completed)
for _ in range(60):
    resp = client.get(f"/api/run/{run_id}")
    if resp.json()["status"] != "running":
        break
    time.sleep(1)

r = resp.json()
assert r["status"] == "completed", f"status: {r['status']}"
events = r.get("events", [])
all_text = " ".join(e.get("text","") for e in events if e.get("kind") in ("text","text_delta"))
assert "E2E_OK" in all_text, f"No E2E_OK: {all_text[:80]}"
print(f"  [OK] GET /api/run/{run_id}: completed, events={len(events)}")

# 4. POST /api/run/{id}/continue
resp = client.post(f"/api/run/{run_id}/continue", json={
    "prompt": "What did you just say? Reply with just the word.",
})
assert resp.status_code == 200, f"continue: {resp.status_code}"

for _ in range(60):
    resp = client.get(f"/api/run/{run_id}")
    if resp.json()["status"] != "running":
        break
    time.sleep(1)

r = resp.json()
all_text = " ".join(e.get("text","") for e in r.get("events",[]) if e.get("kind") in ("text","text_delta"))
assert "E2E_OK" in all_text, f"Resume forgot: {all_text[:80]}"
print(f"  [OK] POST /api/run/{run_id}/continue: remembered")

# 5. POST /api/run/{id}/stop
resp = client.post("/api/run", json={
    "prompt": "Write a Python script that prints 1 to 1000.",
    "model": "claude-sonnet-4.6",
})
run_id2 = resp.json()["run_id"]
time.sleep(3)
try:
    resp = client.post(f"/api/run/{run_id2}/stop")
except RuntimeError:
    pass  # TestClient event loop 关闭是已知问题
r = client.get(f"/api/run/{run_id2}")
assert r.json()["status"] == "stopped", f"stop: {r.json()['status']}"
print(f"  [OK] POST /api/run/{run_id2}/stop: stopped")

# 6. Export
resp = client.get(f"/api/run/{run_id}/export?format=md")
assert resp.status_code == 200
print(f"  [OK] GET /api/run/{run_id}/export")

# 7. List runs
resp = client.get("/api/runs")
assert resp.status_code == 200
print(f"  [OK] GET /api/runs: {len(resp.json())} runs")

# 8. Tree
resp = client.get("/api/tree")
assert resp.status_code == 200
print(f"  [OK] GET /api/tree: {len(resp.json())} roots")

# 9. Workspaces
resp = client.get("/api/workspaces")
assert resp.status_code == 200
print(f"  [OK] GET /api/workspaces: {len(resp.json())} workspaces")

# 10. Label
resp = client.post(f"/api/run/{run_id}/label", json={"label": "e2e-test"})
assert resp.status_code == 200, f"label: {resp.status_code} {resp.text}"
# 短暂等待确保 RunInfo 刷新
time.sleep(0.5)
r = client.get(f"/api/run/{run_id}")
assert r.json().get("label") == "e2e-test", f"label not set: {r.json().get('label')}"
print(f"  [OK] POST /api/run/{run_id}/label")

# 11. Clear
resp = client.post(f"/api/run/{run_id}/clear")
assert resp.status_code == 200
print(f"  [OK] POST /api/run/{run_id}/clear")

# 12. Delete
resp = client.delete(f"/api/run/{run_id2}")
assert resp.status_code == 200
resp = client.get(f"/api/run/{run_id2}")
assert resp.status_code == 404
print(f"  [OK] DELETE /api/run/{run_id2}")

print(f"\n{'='*50}")
print(f"  ALL TESTS PASSED")
print(f"{'='*50}")
