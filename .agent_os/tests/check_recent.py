import urllib.request, json

r = urllib.request.urlopen("http://127.0.0.1:8420/api/agents")
d = json.loads(r.read())
agents = d["agents"]

# 按 started_at 排序找最新的
sorted_agents = sorted(agents, key=lambda x: x.get("started_at", ""), reverse=True)
for a in sorted_agents[:5]:
    aid = a["agent_id"]
    st = a["status"]
    prompt = a.get("prompt", "")[:80]
    print(f"{aid}: {st} | {prompt}")

# 找 "Say hello" 或 "Count from" 的 agent
target = None
for a in agents:
    prompt = a.get("prompt", "")
    if "Say hello" in prompt or "Count from" in prompt:
        target = a
        break

if target:
    aid = target["agent_id"]
    print(f"\nFound target: {aid}")
    r2 = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{aid}")
    d2 = json.loads(r2.read())
    print(f"status: {d2['status']}, model: {d2.get('model')}")
    print(f"children: {d2.get('children_ids')}")
    for ev in d2["events"]:
        kind = ev.get("kind", "")
        text = ev.get("text", "")[:200]
        if kind in ("text", "system"):
            print(f"  [{kind}] {text}")
else:
    print("No target agent found")
