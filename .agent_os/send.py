#!/usr/bin/env python3
"""
send.py — 子 agent 调用此脚本向主 agent 发送中间消息（不结束任务）。

用法：
    python .agent_os/send.py --msg "中间进度汇报或任何信息"

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

    run_id = os.environ.get("AGENT_OS_PARENT_RUN_ID") or os.environ.get("AGENT_OS_RUN_ID", "")
    if not run_id:
        print("[Agent OS] Warning: no target run_id", file=sys.stderr)

    payload = {"run_id": run_id, "msg": args.msg}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/api/run/{run_id}/send",
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
