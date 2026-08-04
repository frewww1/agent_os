import urllib.request, json, time

# create agent
data = json.dumps({"prompt": "Say: EXPORT_DEBUG"}).encode()
req = urllib.request.Request("http://127.0.0.1:8420/api/agent", data=data,
    headers={"Content-Type": "application/json"})
aid = json.loads(urllib.request.urlopen(req, timeout=30).read())["agent_id"]
print(f"Agent: {aid}")

# wait
for i in range(20):
    time.sleep(3)
    r = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{aid}")
    d = json.loads(r.read())
    if d["status"] != "running":
        break

# export
r = urllib.request.urlopen(f"http://127.0.0.1:8420/api/agent/{aid}/export?format=json")
d = json.loads(r.read())
print(f"Export status: {d.get('status')}")
print(f"Export agent_id: {d.get('agent_id')}")
print(f"Keys: {sorted(d.keys())}")
