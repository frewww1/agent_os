"""快速端到端测试 — 逐步输出"""
import urllib.request, json, time, sys

BASE = "http://127.0.0.1:8420"
ok = 0; fail = 0

def T(name, cond):
    global ok, fail
    ok += 1 if cond else 0
    fail += 0 if cond else 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}", flush=True)

def api(method, path, data=None, timeout=10):
    url = f"{BASE}{path}"
    p = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=p, headers={"Content-Type":"application/json"}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: b = json.loads(e.read())
        except: b = {}
        return {"error": str(e), "code": e.code, **b}
    except Exception as e:
        return {"error": str(e)}

def new(prompt, **kw):
    return api("POST", "/api/agent", data=dict(prompt=prompt, **kw)).get("agent_id","")

def wait(aid, t=30):
    t0 = time.time()
    while time.time() - t0 < t:
        d = api("GET", f"/api/agent/{aid}", timeout=5)
        if d.get("error") or d.get("status") != "running":
            return d
        time.sleep(2)
    return {"error": "timeout", "status": "timeout"}

# ========================================
print("=== API ===")
T("agents", isinstance(api("GET","/api/agents").get("agents"), list))
T("models", "deepseek-v4-pro" in api("GET","/api/models").get("models",[]))
T("tree", isinstance(api("GET","/api/tree").get("tree"), list))
T("dag", isinstance(api("GET","/api/dag/templates").get("templates"), list))
T("404", api("GET","/api/agent/xxx").get("code")==404)
T("422", api("POST","/api/agent",data={}).get("code")==422)

# ========================================
print("=== Simple ===", flush=True)
aid = new("Reply: HELLO")
T("created", bool(aid))
d = wait(aid, 90)
T("completed", d.get("status")=="completed")
T("deepseek", d.get("model")=="deepseek-v4-pro")
T("HELLO", any("HELLO" in str(e) for e in d.get("events",[])))

# ========================================
print("=== Goal ===", flush=True)
aid = new("Reply: SUCCESS", goal="Should reply SUCCESS", system_prompt="One word.")
T("created", bool(aid))
d = wait(aid, 120)
kids = d.get("children_ids", [])
T("goal child", len(kids) > 0)
T("SUCCESS", any("SUCCESS" in str(e) for e in d.get("events",[])))
if kids:
    time.sleep(5)
    T("child ok", api("GET", f"/api/agent/{kids[0]}").get("status") is not None)

# ========================================
print("=== Label ===", flush=True)
aid = new("Say: LABEL"); wait(aid)
T("set", api("POST", f"/api/agent/{aid}/label", data={"label":"X"}).get("ok"))
T("get", api("GET", f"/api/agent/{aid}").get("label")=="X")

# ========================================
print("=== Export ===", flush=True)
aid = new("Say: EXPORT"); wait(aid)
r = urllib.request.urlopen(f"{BASE}/api/agent/{aid}/export?format=json", timeout=10)
T("json", json.loads(r.read()).get("agent_id")==aid)
r = urllib.request.urlopen(f"{BASE}/api/agent/{aid}/export?format=md", timeout=10)
T("md", "EXPORT" in r.read().decode())

# ========================================
print("=== Delete ===", flush=True)
aid = new("Say: DEL"); wait(aid)
T("del", api("DELETE", f"/api/agent/{aid}").get("deleted",0)>0)
T("gone", api("GET", f"/api/agent/{aid}").get("code")==404)

print(f"\n{'='*30}\n  {ok}/{ok+fail} passed ({fail} failed)\n{'='*30}", flush=True)
