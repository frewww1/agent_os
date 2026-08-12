#!/usr/bin/env python3
"""
report.py — 子 agent 调用此脚本标记任务完成并汇报最终结果。

用法：
    python report.py --result "最终执行结果摘要"

OS 通过环境变量自动管理 agent_id 和 port，agent 无需关心。
调用后，OS 会标记你的任务为已完成。当所有并行子 agent 都完成后，主 agent 会被唤醒。
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error


def _get_agent_id():
    """获取当前 agent_id：优先环境变量，备选 marker 文件。"""
    agent_id = os.environ.get("AGENT_OS_AGENT_ID", "")
    if agent_id:
        return agent_id
    # Fallback: marker file
    marker = os.path.join(os.getcwd(), ".agent_os_agent_id")
    if os.path.exists(marker):
        with open(marker, "r") as f:
            return f.read().strip()
    return ""


def main():
    parser = argparse.ArgumentParser(description="Report result and mark task complete")
    parser.add_argument("--result", required=True, help="Final result summary")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENT_OS_PORT", "8420")))
    args = parser.parse_args()

    agent_id = _get_agent_id()
    if not agent_id:
        print("[Agent OS] Error: Cannot determine agent_id (env AGENT_OS_AGENT_ID not set, no marker file)", file=sys.stderr)
        sys.exit(1)

    payload = {"agent_id": agent_id, "result": args.result}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/api/agent/{agent_id}/report",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        urllib.request.urlopen(req, timeout=5)
        print(f"[Agent OS] Task completed. Result reported.")
    except Exception as e:
        print(f"[Agent OS] Warning: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
