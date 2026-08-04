import urllib.request, json

# goal agent
r = urllib.request.urlopen("http://127.0.0.1:8420/api/agent/640649e500")
d = json.loads(r.read())
print(f"Goal status: {d['status']}")
for ev in d["events"]:
    kind = ev.get("kind", "")
    text = ev.get("text", "")[:200]
    if kind in ("text", "system", "report"):
        print(f"  [{kind}] {text}")

# parent
r2 = urllib.request.urlopen("http://127.0.0.1:8420/api/agent/b8c4ceaaab")
d2 = json.loads(r2.read())
print(f"\nParent status: {d2['status']}, goal_retries: {d2.get('goal_retries')}")
print(f"goal: {d2.get('goal')}")
