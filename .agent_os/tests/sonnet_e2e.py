"""用 sonnet 验证核心功能"""
import urllib.request, json, time

def api(method, path, data=None):
    url = f"http://127.0.0.1:8420{path}"
    payload = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": str(e), "code": e.code}

def wait(aid, t=60):
    for i in range(t // 3):
        time.sleep(3)
        d = api("GET", f"/api/agent/{aid}")
        if d["status"] != "running":
            return d
    return d

p = f = 0
def check(name, cond):
    global p, f
    (p if cond else f)  # just counting
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if cond: global p; p += 1
    else: global f; f += 1

print("=== Core Tests (claude-sonnet-4.6) ===\n")

# Simple
aid = api("POST", "/api/agent", data={"prompt": "Reply: OK", "model": "claude-sonnet-4.6"})["agent_id"]
d = wait(aid)
check("simple completes", d["status"] == "completed")
check("sonnet model", d.get("model") == "claude-sonnet-4.6")
check("replied OK", "OK" in str(d.get("events", [])))

# Goal
aid = api("POST", "/api/agent", data={
    "prompt": "Reply: PASS", "goal": "Agent should reply with PASS",
    "model": "claude-sonnet-4.6", "system_prompt": "Reply with one word.",
})["agent_id"]
d = wait(aid, 90)
check("goal: child created", len(d.get("children_ids", [])) > 0)
check("goal: replied PASS", "PASS" in str(d.get("events", [])))
for cid in d.get("children_ids", []):
    time.sleep(3)
    cd = api("GET", f"/api/agent/{cid}")
    check("goal: child accessible", cd.get("status") is not None)
    break

# Supervisor
aid = api("POST", "/api/agent", data={
    "prompt": "Write: Hello World", "model": "claude-sonnet-4.6",
    "supervisor": "Verify greeting was written.",
})["agent_id"]
d = wait(aid, 120)
check("sup: child created", len(d.get("children_ids", [])) > 0)

# Continue
aid = api("POST", "/api/agent", data={
    "prompt": "Remember: 42. Reply: OK.", "model": "claude-sonnet-4.6",
})["agent_id"]
d = wait(aid)
sid = d.get("session_id")
check("continue: has session", sid is not None)
api("POST", f"/api/agent/{aid}/continue", data={
    "prompt": "What number? Reply with number only.", "model": "claude-sonnet-4.6"
})
d = wait(aid, 90)
check("continue: remembered 42", "42" in str(d.get("events", [])))

# Label
aid = api("POST", "/api/agent", data={
    "prompt": "Reply: OK", "model": "claude-sonnet-4.6",
})["agent_id"]
wait(aid)
api("POST", f"/api/agent/{aid}/label", data={"label": "Sonnet Test"})
d = api("GET", f"/api/agent/{aid}")
check("label persisted", d.get("label") == "Sonnet Test")

# Delete
aid = api("POST", "/api/agent", data={
    "prompt": "Reply: OK", "model": "claude-sonnet-4.6",
})["agent_id"]
wait(aid)
d = api("DELETE", f"/api/agent/{aid}")
check("delete works", d.get("deleted", 0) > 0)

# Export
aid = api("POST", "/api/agent", data={
    "prompt": "Reply: OK", "model": "claude-sonnet-4.6",
})["agent_id"]
wait(aid)
r = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{aid}/export?format=json")
ex = json.loads(r.read())
check("export has agent_id", ex.get("agent_id") == aid)

print(f"\nResults: {p} passed, {f} failed")
