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
    return api("POST","/api/agent",data=dict(prompt=prompt,**kw)).get("agent_id","")

def wait(aid, t=60):
    t0 = time.time()
    while time.time()-t0 < t:
        d = api("GET",f"/api/agent/{aid}",timeout=5)
        if d.get("error") or d.get("status")!="running": return d
        time.sleep(2)
    return {"error":"timeout"}

# ====== 1. API ======
print("=== 1. API ===", flush=True)
T("GET /api/agents", isinstance(api("GET","/api/agents").get("agents"), list))
T("GET /api/models has deepseek", "deepseek-v4-pro" in api("GET","/api/models").get("models",[]))
T("GET /api/tree", isinstance(api("GET","/api/tree").get("tree"), list))
T("GET /api/dag/templates", isinstance(api("GET","/api/dag/templates").get("templates"), list))

# ====== 2. Simple Agent ======
print("=== 2. Simple ===", flush=True)
aid = new("Reply: HELLO")
T("created", bool(aid))
d = wait(aid, 90)
T("completed", d.get("status")=="completed")
T("deepseek", d.get("model")=="deepseek-v4-pro")
T("HELLO", any("HELLO" in str(e) for e in d.get("events",[])))

# ====== 3. Goal Agent ======
print("=== 3. Goal ===", flush=True)
aid = new("Reply: SUCCESS", goal="Should reply SUCCESS", system_prompt="One word.")
T("created", bool(aid))
d = wait(aid, 120)
kids = d.get("children_ids", [])
T("goal child", len(kids) > 0)
T("SUCCESS", any("SUCCESS" in str(e) for e in d.get("events",[])))
if kids:
    time.sleep(5)
    T("child ok", api("GET",f"/api/agent/{kids[0]}").get("status") is not None)

# ====== 4. Label + Export + Delete ======
print("=== 4. Label/Export/Delete ===", flush=True)
aid = new("Say: LABEL"); wait(aid)
T("label set", api("POST",f"/api/agent/{aid}/label",data={"label":"X"}).get("ok"))
T("label get", api("GET",f"/api/agent/{aid}").get("label")=="X")
aid = new("Say: EXPORT"); wait(aid)
r = urllib.request.urlopen(f"{BASE}/api/agent/{aid}/export?format=json",timeout=10)
T("export json", json.loads(r.read()).get("agent_id")==aid)
r = urllib.request.urlopen(f"{BASE}/api/agent/{aid}/export?format=md",timeout=10)
T("export md", "EXPORT" in r.read().decode())
aid = new("Say: DEL"); wait(aid)
T("delete", api("DELETE",f"/api/agent/{aid}").get("deleted",0)>0)
T("404 after del", api("GET",f"/api/agent/{aid}").get("code")==404)

# ====== 5. DAG code_review（含 goal + supervisor + 多 step） ======
print("=== 5. DAG ===", flush=True)
d = api("POST","/dag/start",data={"template_id":"code_review","workspace_name":f"dag_test_{int(time.time())}"})
aid = d.get("agent_id","")
T("dag created", bool(aid))
if aid:
    T("dag ok", d.get("error") is None)
    # 简单验证：agent 开始运行
    time.sleep(5)
    d2 = api("GET",f"/api/agent/{aid}")
    T("dag running", d2.get("status") in ("running","waiting"))
    T("dag has workspace", bool(d2.get("workspace_path")))
else:
    T("dag start", False)

# ====== Summary ======
total = ok + fail
print(f"\n{'='*30}\n  {ok}/{total} passed ({fail} failed)\n{'='*30}", flush=True)
sys.exit(0 if fail == 0 else 1)
