#!/usr/bin/env python3
"""
send.py — 子 agent 调用此脚本向主 agent 发送中间消息（不结束任务）。

用法：
    python send.py --msg "中间进度汇报或任何信息"

注意：这只是发送消息，不会结束你的任务。要结束任务请用 report.py。
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error


def main():
    parser = argparse.ArgumentParser(description="Send message to OS (does not end task)")
    parser.add_argument("--msg", required=True, help="Message content")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENT_OS_PORT", "8420")))
    args = parser.parse_args()

    agent_id = os.environ.get("AGENT_OS_AGENT_ID", "")
    if not agent_id:
        print("[Agent OS] Warning: AGENT_OS_AGENT_ID not set", file=sys.stderr)

    payload = {"agent_id": agent_id, "msg": args.msg}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/api/agent/{agent_id}/send",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        urllib.request.urlopen(req, timeout=5)
        print(f"[Agent OS] Message sent.")
    except Exception as e:
        print(f"[Agent OS] Warning: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
