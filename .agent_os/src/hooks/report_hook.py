"""Hook 脚本 — 子 Agent 汇报结果到 OS。"""
import json, os, sys, urllib.request

PORT = os.environ.get("AGENT_OS_PORT", "8420")
RUN_ID = os.environ.get("AGENT_OS_RUN_ID", "")
BASE = f"http://127.0.0.1:{PORT}"


def main():
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except Exception:
        return

    tool_input = hook_input.get("tool_input", {})
    result = tool_input.get("result", "") or tool_input.get("message", "")

    if not result:
        return

    try:
        req = urllib.request.Request(
            f"{BASE}/api/run/{RUN_ID}/report",
            data=json.dumps({"result": result}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    main()
