"""Goal agent 端到端测试"""
import urllib.request, json, time

payload = json.dumps({
    "prompt": "Reply with exactly: YES",
    "goal": "Agent should reply with YES",
    "system_prompt": "You are concise. Reply with one word only.",
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8420/api/agent",
    data=payload,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=30)
aid = json.loads(resp.read())["agent_id"]
print(f"Created: {aid}")

for i in range(20):
    time.sleep(3)
    r = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{aid}")
    d = json.loads(r.read())
    st = d["status"]
    kids = d.get("children_ids", [])
    print(f"  [{i}] status={st}, children={kids}, model={d.get('model')}")
    if st != "running":
        for ev in d["events"]:
            kind = ev.get("kind", "")
            text = ev.get("text", "")[:200]
            if kind in ("text", "system", "error"):
                print(f"  [{kind}] {text}")
        # 检查 goal 子 agent
        for cid in kids:
            time.sleep(2)
            r2 = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{cid}")
            dc = json.loads(r2.read())
            print(f"  Goal child {cid}: status={dc['status']}")
            for ev in dc["events"]:
                kind = ev.get("kind", "")
                text = ev.get("text", "")[:200]
                if kind in ("text", "system"):
                    print(f"    [{kind}] {text}")
        break

print("DONE")
