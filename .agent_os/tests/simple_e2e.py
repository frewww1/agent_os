"""最简单的端到端测试"""
import urllib.request, json, time

# 创建一个 agent
payload = json.dumps({"prompt": "Reply with: OK"}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8420/api/agent",
    data=payload,
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req, timeout=30)
result = json.loads(resp.read())
aid = result["agent_id"]
print(f"Created agent: {aid}")

# 轮询状态
for i in range(15):
    time.sleep(3)
    r = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{aid}")
    d = json.loads(r.read())
    st = d["status"]
    model = d.get("model")
    print(f"  [{i}] status={st}, model={model}")
    if st != "running":
        for ev in d["events"]:
            kind = ev.get("kind", "")
            text = ev.get("text", "")[:200]
            if kind in ("text", "error"):
                print(f"  [{kind}] {text}")
        break

print("DONE")
