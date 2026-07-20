"""Agent OS MCP Server — 为子 agent 提供 os_spawn / os_report / os_send 三个 MCP tool。

每个 tool 内部 HTTP POST 到 OS Dashboard API，与 spawn.py/report.py/send.py 走同一套端点。
"""
import json
import os
import urllib.request
import urllib.error
import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("agent_os.mcp")

# 从环境变量获取 OS 端口和当前 run_id
OS_PORT = int(os.environ.get("AGENT_OS_PORT", "8420"))
OS_RUN_ID = os.environ.get("AGENT_OS_RUN_ID", "")
OS_PARENT_RUN_ID = os.environ.get("AGENT_OS_PARENT_RUN_ID", "")
OS_BASE_URL = f"http://127.0.0.1:{OS_PORT}"

mcp = FastMCP(
    "agent-os",
    instructions="Agent OS 子 agent 通信工具。用于 spawn 子 agent、发送中间消息、汇报最终结果。",
)


def _post(path: str, payload: dict) -> dict:
    """HTTP POST 到 OS Dashboard API。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OS_BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"error": str(e)}


# ---- MCP Tools ----

@mcp.tool()
def os_spawn(
    tasks: str,
    wait_strategy: str = "all",
) -> str:
    """创建子 agent 执行任务。

    Args:
        tasks: JSON 数组字符串，每个元素 {"prompt": "任务描述", "agent_name": "可选",
               "type": "generative|interactive", "model": "可选模型名"}
        wait_strategy: 等待策略，"all"(全部完成) 或 "any"(任一完成)

    Returns:
        包含 spawn_id 和 child_run_ids 的 JSON 字符串。
    """
    try:
        tasks_list = json.loads(tasks) if isinstance(tasks, str) else tasks
    except json.JSONDecodeError:
        return json.dumps({"error": f"Invalid JSON tasks: {tasks[:100]}"})

    parent_run_id = OS_RUN_ID
    parent_session_id = os.environ.get("AGENT_OS_SESSION_ID", "")

    payload = {
        "parent_run_id": parent_run_id,
        "parent_session_id": parent_session_id,
        "tasks": tasks_list,
        "wait_strategy": wait_strategy,
    }
    result = _post("/api/spawn", payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def os_report(result: str) -> str:
    """汇报任务完成并标记结束。调用后父 agent 会被唤醒。

    Args:
        result: 最终结果摘要（会传给父 agent）。

    Returns:
        确认信息。
    """
    if not OS_RUN_ID:
        return json.dumps({"error": "AGENT_OS_RUN_ID not set"})

    payload = {"run_id": OS_RUN_ID, "result": result}
    resp = _post(f"/api/run/{OS_RUN_ID}/report", payload)
    return json.dumps(resp, ensure_ascii=False)


@mcp.tool()
def os_send(msg: str) -> str:
    """发送中间进度消息给父 agent（不结束任务）。

    Args:
        msg: 消息内容。

    Returns:
        确认信息。
    """
    if not OS_RUN_ID:
        return json.dumps({"error": "AGENT_OS_RUN_ID not set"})

    payload = {"run_id": OS_RUN_ID, "msg": msg}
    resp = _post(f"/api/run/{OS_RUN_ID}/send", payload)
    return json.dumps(resp, ensure_ascii=False)


def run():
    """启动 MCP Server（stdio 模式，供 CLI 通过 --mcp-config 连接）。"""
    mcp.run(transport="stdio")
