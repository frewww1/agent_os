#!/usr/bin/env python3
"""
autonomous-loop.py — 自主循环状态管理 CLI。

调度 agent 在 skill 的指导下调用此脚本管理循环状态：
    python .agent_os/autonomous-loop.py --init '<goal_json>'
        初始化循环状态，写入 goal + 从 dag.json 加载的 steps。

    python .agent_os/autonomous-loop.py --status
        打印当前循环状态（JSON），含所有 step 的状态、重试次数、结果。

    python .agent_os/autonomous-loop.py --current
        打印当前待执行的 step 对象（JSON），如果全部完成则打印 {"done": true}。

    python .agent_os/autonomous-loop.py --mark-result <step_id> <PASS|FAIL> '<reason>'
        记录 step 的执行结果。PASS→下一步；FAIL→记录重试（含 feedback）。

    python .agent_os/autonomous-loop.py --get-feedback <step_id>
        打印该 step 的失败反馈（eval reason），用于重试时注入 executor prompt。

    python .agent_os/autonomous-loop.py --is-done
        打印 {"done": true} 或 {"done": false}，用于循环终止判断。

状态文件位置：$AGENT_OS_WORKSPACE/loop_state.json（兜底当前目录）。
"""
import argparse
import json
import os
import sys
from datetime import datetime


def _workspace() -> str:
    return os.environ.get("AGENT_OS_WORKSPACE", "") or os.getcwd()


def _state_path() -> str:
    return os.path.join(_workspace(), "loop_state.json")


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        print(f"[loop] state file not found: {path}", file=sys.stderr)
        print(f"[loop] run --init first", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    path = _state_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _log(state: dict, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    state.setdefault("log", []).append(f"[{ts}] {msg}".strip())
    _save_state(state)


def _load_dag_steps(ws: str) -> list[dict]:
    """从 dag.json 加载 step 模板，转为 loop step 格式。"""
    dag_path = os.path.join(ws, "dag.json")
    if not os.path.exists(dag_path):
        return []
    with open(dag_path, "r", encoding="utf-8") as f:
        dag = json.load(f)
    steps = []
    for s in dag.get("steps", []):
        steps.append({
            "id": s["id"],
            "name": s.get("name", s["id"]),
            "prompt": s.get("prompt", ""),
            "agent_name": s.get("agent_name"),
            "model": s.get("model"),
            "depends_on": s.get("depends_on", []),
            "status": "pending",
            "retries": 0,
            "max_retries": 3,
            "eval_result": None,
            "eval_reason": None,
            "error_feedback": None,
            "completed_at": None,
            "executor_output": None,
        })
    return steps


def cmd_init(args) -> None:
    """--init '{"goal":"...", "steps_override":[...]}'"""
    ws = _workspace()
    try:
        init_data = json.loads(args.init_json)
    except json.JSONDecodeError as e:
        print(f"[loop] invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    goal = init_data.get("goal", "")
    if not goal:
        print("[loop] --init requires 'goal' in JSON", file=sys.stderr)
        sys.exit(1)

    steps_override = init_data.get("steps_override")
    if steps_override:
        steps = steps_override
    else:
        steps = _load_dag_steps(ws)

    max_turns = init_data.get("max_turns", 0) or 20  # 默认 20 轮兜底

    state = {
        "goal": goal,
        "state": "init",  # init → running → done | failed | blocked
        "steps": steps,
        "max_turns": max_turns,
        "turn_count": 0,
        "created_at": datetime.now().isoformat(),
        "log": [f"[{datetime.now().strftime('%H:%M:%S')}] 初始化: {goal[:100]} (max_turns={max_turns})"],
    }
    _save_state(state)
    print(json.dumps({"ok": True, "step_count": len(steps), "max_turns": max_turns}, ensure_ascii=False))


def cmd_status(args) -> None:
    """--status"""
    state = _load_state()
    # 简洁输出：只返回 steps 的摘要 + 全局状态
    summary = {
        "state": state.get("state", "unknown"),
        "goal": state.get("goal", ""),
        "total": len(state.get("steps", [])),
        "done": sum(1 for s in state.get("steps", []) if s["status"] == "pass"),
        "failed": sum(1 for s in state.get("steps", []) if s["status"] == "fail"),
        "blocked": sum(1 for s in state.get("steps", []) if s["status"] == "blocked"),
        "pending": sum(1 for s in state.get("steps", []) if s["status"] == "pending"),
        "running": sum(1 for s in state.get("steps", []) if s["status"] == "running"),
        "steps": [
            {
                "id": s["id"],
                "name": s["name"],
                "status": s["status"],
                "retries": s["retries"],
                "eval_result": s.get("eval_result"),
            }
            for s in state.get("steps", [])
        ],
    }
    print(json.dumps(summary, ensure_ascii=False))


def cmd_current(args) -> None:
    """--current：返回当前待执行的 step，同时递增 turn 计数器。"""
    state = _load_state()
    steps = state.get("steps", [])

    # 递增全局回合数
    state["turn_count"] = state.get("turn_count", 0) + 1
    max_turns = state.get("max_turns", 20)

    # 全局回合上限检查
    if state["turn_count"] > max_turns:
        state["state"] = "blocked"
        _log(state, f"已达全局回合上限 ({max_turns})，终止")
        _save_state(state)
        print(json.dumps({"done": True, "all_pass": False, "blocked": True,
                          "reason": f"max_turns ({max_turns}) exceeded"},
                         ensure_ascii=False))
        return

    # 检查是否有 blocked 的 step（整个循环应该终止）
    blocked = [s for s in steps if s["status"] == "blocked"]
    if blocked:
        print(json.dumps({"blocked": True, "step": blocked[0]}, ensure_ascii=False))
        return

    # 找到第一个 pending 或 fail（需要重试）的 step
    for s in steps:
        if s["status"] in ("pending", "fail"):
            # 如果是 fail 但已达最大重试次数，标记 blocked
            if s["status"] == "fail" and s["retries"] >= s.get("max_retries", 3):
                s["status"] = "blocked"
                state["state"] = "blocked"
                _log(state, f"步骤 {s['id']} 已达最大重试次数({s['retries']})，标记 blocked")
                print(json.dumps({"blocked": True, "step": s}, ensure_ascii=False))
                return
            s["status"] = "running"
            state["state"] = "running"
            _log(state, f"执行步骤: {s['id']} (第 {s['retries']+1} 次尝试) [turn {state['turn_count']}/{max_turns}]")
            print(json.dumps(s, ensure_ascii=False))
            return

    # 全部 pass 或 done
    all_pass = all(s["status"] == "pass" for s in steps)
    state["state"] = "done"
    _save_state(state)
    print(json.dumps({"done": True, "all_pass": all_pass}, ensure_ascii=False))


def cmd_mark_result(args) -> None:
    """--mark-result <step_id> <PASS|FAIL|IMPOSSIBLE> '<reason>'"""
    state = _load_state()
    step_id = args.step_id
    result = args.result.upper()

    if result not in ("PASS", "FAIL", "IMPOSSIBLE"):
        print(f"[loop] result must be PASS / FAIL / IMPOSSIBLE, got: {result}", file=sys.stderr)
        sys.exit(1)

    for s in state.get("steps", []):
        if s["id"] == step_id:
            if result == "PASS":
                s["status"] = "pass"
                s["eval_result"] = "PASS"
                s["eval_reason"] = args.reason
                s["completed_at"] = datetime.now().isoformat()
                _log(state, f"步骤 {step_id}: PASS — {args.reason[:200]}")
            elif result == "IMPOSSIBLE":
                # 不可达：直接 blocked，不重试
                s["status"] = "blocked"
                s["eval_result"] = "IMPOSSIBLE"
                s["eval_reason"] = args.reason
                s["error_feedback"] = f"[IMPOSSIBLE] {args.reason}"
                state["state"] = "blocked"
                _log(state, f"步骤 {step_id}: IMPOSSIBLE → BLOCKED — {args.reason[:200]}")
            else:  # FAIL
                s["retries"] += 1
                s["eval_result"] = "FAIL"
                s["eval_reason"] = args.reason
                s["error_feedback"] = args.reason
                if s["retries"] >= s.get("max_retries", 3):
                    s["status"] = "blocked"
                    state["state"] = "blocked"
                    _log(state, f"步骤 {step_id}: FAIL (重试 {s['retries']}/{s['max_retries']}) → BLOCKED — {args.reason[:200]}")
                else:
                    s["status"] = "fail"
                    _log(state, f"步骤 {step_id}: FAIL (重试 {s['retries']}/{s['max_retries']}) — {args.reason[:200]}")
            print(json.dumps({"ok": True, "step_id": step_id, "result": result}, ensure_ascii=False))
            return

    print(f"[loop] step not found: {step_id}", file=sys.stderr)
    sys.exit(1)


def cmd_get_feedback(args) -> None:
    """--get-feedback <step_id>：获取失败反馈，用于重试。"""
    state = _load_state()
    for s in state.get("steps", []):
        if s["id"] == args.step_id:
            print(json.dumps({
                "step_id": s["id"],
                "retries": s["retries"],
                "max_retries": s.get("max_retries", 3),
                "feedback": s.get("error_feedback") or s.get("eval_reason") or "",
                "last_executor_output": s.get("executor_output", ""),
            }, ensure_ascii=False))
            return
    print(f"[loop] step not found: {args.step_id}", file=sys.stderr)
    sys.exit(1)


def cmd_set_executor_output(args) -> None:
    """--set-executor-output <step_id> '<output>'
    在执行 eval 之前，把 executor 的输出存入 state，供 eval agent 引用。"""
    state = _load_state()
    for s in state.get("steps", []):
        if s["id"] == args.step_id:
            s["executor_output"] = args.output
            _save_state(state)
            print(json.dumps({"ok": True, "step_id": args.step_id}, ensure_ascii=False))
            return
    print(f"[loop] step not found: {args.step_id}", file=sys.stderr)
    sys.exit(1)


def cmd_is_done(args) -> None:
    """--is-done"""
    state = _load_state()
    steps = state.get("steps", [])
    blocked = any(s["status"] == "blocked" for s in steps)
    if len(steps) == 0:
        all_pass = True  # vacuous truth: 0 steps → all passed
    else:
        all_pass = all(s["status"] == "pass" for s in steps)
    print(json.dumps({
        "done": all_pass or blocked,
        "all_pass": all_pass,
        "blocked": blocked,
        "state": state.get("state", "unknown"),
    }, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Autonomous Loop State Manager")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialize loop state")
    p_init.add_argument("init_json", help='JSON: {"goal":"...", "steps_override":[...]}')

    sub.add_parser("status", help="Print loop status")

    sub.add_parser("current", help="Print current pending step")

    p_mark = sub.add_parser("mark-result", help="Record step execution result")
    p_mark.add_argument("step_id", help="Step ID")
    p_mark.add_argument("result", choices=["PASS", "FAIL", "IMPOSSIBLE"], help="Evaluation result")
    p_mark.add_argument("reason", help="Evaluation reason (quoted string)")

    p_fb = sub.add_parser("get-feedback", help="Get failure feedback for retry")
    p_fb.add_argument("step_id", help="Step ID")

    p_seo = sub.add_parser("set-executor-output", help="Store executor output for eval")
    p_seo.add_argument("step_id", help="Step ID")
    p_seo.add_argument("output", help="Executor output text")

    sub.add_parser("is-done", help="Check if all steps complete")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "current":
        cmd_current(args)
    elif args.command == "mark-result":
        cmd_mark_result(args)
    elif args.command == "get-feedback":
        cmd_get_feedback(args)
    elif args.command == "set-executor-output":
        cmd_set_executor_output(args)
    elif args.command == "is-done":
        cmd_is_done(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
