"""全面端到端真实测试 — 带超时保护"""
import urllib.request, json, time, sys

BASE = "http://127.0.0.1:8420"
passed = 0
failed = 0

def api(method, path, data=None, timeout=30):
    url = f"{BASE}{path}"
    payload = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = {}
        try: body = json.loads(e.read())
        except: pass
        return {"error": str(e), "code": e.code, **body}
    except Exception as e:
        return {"error": str(e)}

def wait_agent(aid, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = api("GET", f"/api/agent/{aid}", timeout=10)
        if d.get("error") or d.get("status") != "running":
            return d
        time.sleep(3)
    return {"error": "timeout"}

def test(name, condition):
    global passed, failed
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if condition: passed += 1
    else: failed += 1

# ============================================================
# 1. API Endpoints
# ============================================================
print("=== 1. API Endpoints ===")
d = api("GET", "/api/agents")
test("/api/agents returns list", isinstance(d.get("agents"), list))

d = api("GET", "/api/models")
test("/api/models includes deepseek", "deepseek-v4-pro" in d.get("models", []))

d = api("GET", "/api/tree")
test("/api/tree returns list", isinstance(d.get("tree"), list))

d = api("GET", "/api/dag/templates")
test("/api/dag/templates returns list", isinstance(d.get("templates"), list))

d = api("GET", "/api/workspaces")
test("/api/workspaces returns list", isinstance(d.get("workspaces"), list))

d = api("GET", "/api/agent/nonexistent")
test("/api/agent/unknown returns 404", d.get("code") == 404 or d.get("error") is not None)

try:
    api("POST", "/api/agent", data={"no_prompt": "test"})
except:
    pass
d = api("POST", "/api/agent", data={})
test("/api/agent POST without prompt returns 422", d.get("code") == 422)

# ============================================================
# 2. Simple Agent
# ============================================================
print("\n=== 2. Simple Agent ===")
aid = api("POST", "/api/agent", data={"prompt": "Reply with exactly: HELLO"}).get("agent_id", "")
test("agent created", bool(aid))
d = wait_agent(aid)
test("agent completes", d.get("status") == "completed")
test("default model is deepseek", d.get("model") == "deepseek-v4-pro")
has_hello = any("HELLO" in str(e) for e in d.get("events", []))
test("agent replied HELLO", has_hello)

# ============================================================
# 3. Goal Agent
# ============================================================
print("\n=== 3. Goal Agent ===")
aid = api("POST", "/api/agent", data={
    "prompt": "Reply with: SUCCESS",
    "goal": "Agent should reply with SUCCESS",
    "system_prompt": "Reply with one word.",
}).get("agent_id", "")
test("goal agent created", bool(aid))
d = wait_agent(aid, timeout=90)
kids = d.get("children_ids", [])
test("goal creates child", len(kids) > 0)
has_success = any("SUCCESS" in str(e) for e in d.get("events", []))
test("agent replied SUCCESS", has_success)
if kids:
    time.sleep(5)
    cd = api("GET", f"/api/agent/{kids[0]}")
    test("goal child accessible", cd.get("status") is not None)

# ============================================================
# 4. Stop Agent
# ============================================================
print("\n=== 4. Stop Agent ===")
aid = api("POST", "/api/agent", data={
    "prompt": "Count from 1 to 100 slowly, one per line.",
}).get("agent_id", "")
test("stop agent created", bool(aid))
time.sleep(5)
d = api("POST", f"/api/agent/{aid}/stop")
test("stop returns ok", d.get("ok") is True)
time.sleep(2)
d = api("GET", f"/api/agent/{aid}")
test("stopped status", d.get("status") in ("stopped", "failed"))

# ============================================================
# 5. Delete Agent
# ============================================================
print("\n=== 5. Delete Agent ===")
aid = api("POST", "/api/agent", data={"prompt": "Say: DELETE_ME"}).get("agent_id", "")
wait_agent(aid)
d = api("DELETE", f"/api/agent/{aid}")
test("delete returns count", d.get("deleted", 0) > 0)
d = api("GET", f"/api/agent/{aid}")
test("deleted returns 404", d.get("code") == 404)

# ============================================================
# 6. Label Agent
# ============================================================
print("\n=== 6. Label Agent ===")
aid = api("POST", "/api/agent", data={"prompt": "Say: LABEL_TEST"}).get("agent_id", "")
wait_agent(aid)
d = api("POST", f"/api/agent/{aid}/label", data={"label": "MyTest"})
test("label returns ok", d.get("ok") is True)
d = api("GET", f"/api/agent/{aid}")
test("label persisted", d.get("label") == "MyTest")

# ============================================================
# 7. Export
# ============================================================
print("\n=== 7. Export ===")
aid = api("POST", "/api/agent", data={"prompt": "Say: EXPORT_TEST"}).get("agent_id", "")
wait_agent(aid)
r = urllib.request.urlopen(f"{BASE}/api/agent/{aid}/export?format=json", timeout=10)
export = json.loads(r.read())
test("export json has agent_id", export.get("agent_id") == aid)

r = urllib.request.urlopen(f"{BASE}/api/agent/{aid}/export?format=md", timeout=10)
md = r.read().decode()
test("export md contains prompt", "EXPORT_TEST" in md)

# ============================================================
# Summary
# ============================================================
total = passed + failed
print(f"\n{'='*40}")
print(f"  {passed}/{total} passed ({failed} failed)")
print(f"{'='*40}")
sys.exit(0 if failed == 0 else 1)
