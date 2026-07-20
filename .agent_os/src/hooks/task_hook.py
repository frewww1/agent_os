"""Hook 脚本 — 拦截 CodeBuddy 原生 Task 工具调用，转发到 OS spawn。

被 CodeBuddy CLI 的 PreToolUse hook 调用，stdin 接收 JSON 输入：
    {"tool_name": "Task", "tool_input": {"prompt": "...", "subagent_type": "...", ...}}

读取 OS 端口和 run_id 从环境变量，HTTP POST 到 /api/spawn。
"""
import json, os, sys, urllib.request, urllib.error

PORT = os.environ.get("AGENT_OS_PORT", "8420")
RUN_ID = os.environ.get("AGENT_OS_RUN_ID", "")
BASE = f"http://127.0.0.1:{PORT}"


def main():
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except Exception as e:
        print(json.dumps({"decision": "allow"}))
        return

    tool_input = hook_input.get("tool_input", {})
    prompt = tool_input.get("prompt", "") or tool_input.get("description", "")
    subagent_type = tool_input.get("subagent_type", "") or tool_input.get("subagent_name", "")

    if not prompt:
        # 没有 prompt，放行让 CodeBuddy 自己处理
        print(json.dumps({"decision": "allow"}))
        return

    # 构建 spawn 请求
    task = {"prompt": prompt}
    if subagent_type:
        task["agent_name"] = subagent_type

    payload = {
        "tasks": [task],
        "wait_strategy": "all",
        "parent_run_id": RUN_ID,
    }

    try:
        req = urllib.request.Request(
            f"{BASE}/api/spawn",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(json.dumps({
            "decision": "block",
            "reason": f"OS spawn failed: {e}",
        }))
        return

    if result.get("error"):
        print(json.dumps({
            "decision": "block",
            "reason": result["error"],
        }))
        return

    # 阻止 CodeBuddy 自己创建子 agent，由 OS 接管
    child_ids = result.get("child_run_ids", [])
    print(json.dumps({
        "decision": "block",
        "reason": f"OS spawned {len(child_ids)} sub-agent(s): {', '.join(c[:8] for c in child_ids)}",
    }))


if __name__ == "__main__":
    main()
