#!/usr/bin/env python3
"""
spawn.py — Agent 调用此脚本通知 OS 派发子 agent。

用法（Agent 在 Bash 中调用，命令路径由系统提示词给出绝对路径）：
    python spawn.py --tasks '[{"prompt":"任务1"},{"prompt":"任务2"}]'
    python spawn.py --tasks '[{"prompt":"...", "agent_name":"策划需求分析智能体"}]' --wait all
    python spawn.py --tasks '[...]' --poll   # 阻塞等待子 agent 完成并返回结果

参数：
    --tasks   JSON 数组，每个元素 {"prompt": "...", "agent_name": "可选",
                                  "type": "generative|interactive",
                                  "model": "sonnet|opus|haiku|完整模型名（可选，默认继承父 agent）"}
    --wait    等待策略: "all"(默认，全部完成后唤醒) | "any"(任一完成即唤醒)
    --poll    阻塞轮询等待所有子 agent 完成，返回结果后退出
              （默认不 poll，立即退出，由 OS 通过 resume 机制唤醒父 agent）
    --timeout 轮询超时秒数（默认 600，仅 --poll 模式生效）

OS 通过环境变量自动管理 parent_agent_id 和 port，agent 无需关心。
脚本行为：
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error


def _get_parent_agent_id(cli_parent: str = "") -> str:
    """获取 parent_agent_id：CLI arg > env var > marker file。"""
    if cli_parent:
        return cli_parent
    agent_id = os.environ.get("AGENT_OS_AGENT_ID", "")
    if agent_id:
        return agent_id
    # Fallback: marker file
    marker = os.path.join(os.getcwd(), ".agent_os_agent_id")
    if os.path.exists(marker):
        with open(marker, "r") as f:
            return f.read().strip()
    return ""


def _api_get(port: int, path: str) -> dict:
    """GET 请求 OS API，返回 JSON。"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception:
        return {}


def _poll_children(port: int, child_ids: list, timeout: int) -> str:
    """轮询子 agent 状态，全部完成后返回结果摘要。"""
    deadline = time.time() + timeout
    results = {}
    
    while time.time() < deadline:
        all_done = True
        for cid in child_ids:
            if cid in results:
                continue
            info = _api_get(port, f"/api/agent/{cid}")
            status = info.get("status", "unknown")
            if status in ("completed", "failed", "stopped"):
                results[cid] = {
                    "status": status,
                    "result": info.get("reported_result") or info.get("_fallback_result") or "(无输出)",
                    "messages": [m.get("msg", "") for m in info.get("messages", [])],
                }
            else:
                all_done = False
        if all_done:
            break
        time.sleep(2)
    
    # 超时处理：未完成的也强制收集
    for cid in child_ids:
        if cid not in results:
            results[cid] = {"status": "timeout", "result": "(超时未完成)", "messages": []}
    
    # 组装输出
    lines = []
    lines.append("")
    lines.append("=" * 40)
    lines.append("子 agent 执行完毕，结果如下：")
    lines.append("")
    for i, cid in enumerate(child_ids, 1):
        r = results[cid]
        lines.append(f"子任务 {i} (ID: {cid[:8]}):")
        lines.append(f"  状态: {r['status']}")
        if r['messages']:
            lines.append("  过程消息:")
            for m in r['messages']:
                lines.append(f"    - {m}")
        lines.append(f"  结果: {r['result']}")
        lines.append("")
    lines.append("=" * 40)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Spawn sub-agents via Agent OS")
    parser.add_argument("--tasks", required=True, help="JSON array of tasks")
    parser.add_argument("--wait", default="all", choices=["all", "any"],
                        help="Wait strategy: all (default) or any")
    parser.add_argument("--poll", action="store_true",
                        help="Block and poll until children complete, print results")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Poll timeout in seconds (default: 600)")
    parser.add_argument("--parent", default="", help="Parent agent_id (auto-detected if not provided)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENT_OS_PORT", "8420")),
                        help="OS Dashboard port")
    args = parser.parse_args()

    # Each task: {"prompt": "...", "agent_name": "optional", "type": "generative|interactive"}
    try:
        tasks = json.loads(args.tasks)
        if not isinstance(tasks, list) or not tasks:
            print("[ERROR] --tasks must be a non-empty JSON array", file=sys.stderr)
            sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid JSON in --tasks: {e}", file=sys.stderr)
        sys.exit(1)

    parent_agent_id = _get_parent_agent_id(args.parent)
    if not parent_agent_id:
        print("[ERROR] Cannot determine parent_agent_id (env AGENT_OS_AGENT_ID not set, no marker file)", file=sys.stderr)
        sys.exit(1)

    parent_session_id = os.environ.get("AGENT_OS_SESSION_ID", "")

    # Build spawn request
    payload = {
        "tasks": tasks,
        "wait_strategy": args.wait,
        "parent_id": parent_agent_id,
        "parent_session_id": parent_session_id,
    }

    # POST to OS
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/api/spawn",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"[ERROR] Cannot reach Agent OS at port {args.port}: {e}", file=sys.stderr)
        print("Make sure Agent OS is running (start.bat / python main.py)", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    # Output confirmation
    spawn_id = result.get("spawn_id", "?")
    child_count = result.get("child_count", len(tasks))
    child_ids = result.get("child_ids", [])

    print(f"[Agent OS] Spawned {child_count} sub-agent(s). Spawn ID: {spawn_id}")
    print(f"[Agent OS] Child IDs: {child_ids}")

    if args.poll:
        print(f"[Agent OS] Polling for completion (timeout={args.timeout}s)...")
        summary = _poll_children(args.port, child_ids, args.timeout)
        print(summary)

    # 如果指定了 --poll，子 agent 完成后自动 report
    if args.poll and child_ids:
        all_results = []
        for cid in child_ids:
            info = _api_get(args.port, f"/api/agent/{cid}")
            all_results.append({
                "agent_id": cid[:8],
                "status": info.get("status", "unknown"),
                "result": info.get("reported_result") or "(无)",
            })
        result_text = json.dumps(all_results, ensure_ascii=False)
        report_payload = {"agent_id": parent_agent_id, "result": result_text}
        report_data = json.dumps(report_payload).encode("utf-8")
        report_req = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/api/agent/{parent_agent_id}/report",
            data=report_data,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(report_req, timeout=5)
            print(f"[Agent OS] Auto-reported results to OS.")
        except Exception:
            pass


if __name__ == "__main__":
    main()
