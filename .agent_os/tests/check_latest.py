import urllib.request, json
r = urllib.request.urlopen("http://127.0.0.1:8420/api/agents")
d = json.loads(r.read())
agents = d["agents"]
if agents:
    latest = agents[0]
    aid = latest["agent_id"]
    st = latest["status"]
    print(f"Latest agent: {aid}, status={st}")
    r2 = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{aid}")
    d2 = json.loads(r2.read())
    model = d2.get("model")
    print(f"model: {model}")
    for ev in d2["events"]:
        kind = ev.get("kind", "")
        text = ev.get("text", "")[:200]
        if kind == "text":
            print(f"  [{kind}] {text}")
