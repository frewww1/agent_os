#!/usr/bin/env python3
"""dag.py — 调度 agent 操作 dag.json 的本地 CLI。

用法（调度 agent 在 Bash 中调用，cwd 即 workspace）：
    python .agent_os/dag.py --ready
        打印当前 ready 的 step 对象数组（JSON，按拓扑序）。每项含
        id / step_id(=id) / prompt / agent_name / model，可直接拷进
        spawn.py 的 --tasks（务必保留 step_id 字段，OS 才会打 step commit）。

    python .agent_os/dag.py --status
        打印所有 step 的状态表（JSON，按拓扑序）：id / name / status /
        depends_on。用于自查「当前跑到哪一步」。

    python .agent_os/dag.py --mark-done <step_id>
        把该 step 的 status 置为 done（带 completed_at），写回 dag.json。

    python .agent_os/dag.py --mark-failed <step_id>
        把该 step 的 status 置为 failed（带 completed_at），写回 dag.json。

    python .agent_os/dag.py --rerun <step_id>
        把该 step + 所有传递下游 status 重置为 pending，写回，
        并打印这批被重置的 step id（JSON 数组，拓扑序）。
        仅改 DAG 状态，不动 workspace 文件。

    python .agent_os/dag.py --add-step '<json>'
        【动态编排】运行时往 dag.json 追加一个新节点。json 形如：
          {"id":"gen_test","name":"测试用例生成","prompt":"...",
           "depends_on":["code_dev"],"agent_name":"测试用例生成智能体",
           "model":null}
        调度 agent 在看到上游产出（如已生成代码）后，用此动态插入下一步，
        然后 --ready 取出、spawn.py 派发。校验失败（id 重复/依赖缺失/成环）
        非零退出并打印原因。

    python .agent_os/dag.py --reset-to <step_id>
        【配合 git 回退】把该 step + 下游重置为 pending（同 --rerun 的状态部分），
        打印受影响 id（JSON 数组）。与 OS 的 /api/run/{id}/dag/checkout 配合：
        API 先 git checkout 回退 workspace 文件，再调本命令同步 DAG 状态。
        如只想本地重置状态不回退文件，用 --rerun 即可。

dag.json 位置：$AGENT_OS_WORKSPACE/dag.json（兜底当前目录）。
注：running 态由 Agent OS 在 spawn 时自动写入（task 带 step_id 即触发），
    调度 agent 不需要手动标记 running。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ..core import dag_planner as dp  # noqa: E402


def _workspace() -> str:
    """定位 workspace：优先 AGENT_OS_WORKSPACE 环境变量，兜底 cwd。"""
    return os.environ.get("AGENT_OS_WORKSPACE", "") or os.getcwd()


def main() -> None:
    parser = argparse.ArgumentParser(description="Operate dag.json for Agent OS")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ready", action="store_true",
                       help="print ready steps as JSON array")
    group.add_argument("--status", action="store_true",
                       help="print all steps' status table as JSON array")
    group.add_argument("--mark-done", metavar="STEP_ID",
                       help="mark a step as done")
    group.add_argument("--mark-failed", metavar="STEP_ID",
                       help="mark a step as failed")
    group.add_argument("--rerun", metavar="STEP_ID",
                       help="reset a step and all its descendants to pending")
    group.add_argument("--add-step", metavar="JSON",
                       help="dynamically append a new step to dag.json")
    group.add_argument("--reset-to", metavar="STEP_ID",
                       help="reset a step and descendants to pending (for git checkout)")
    args = parser.parse_args()

    ws = _workspace()
    dag = dp.load_dag(ws)
    steps = dag.get("steps", [])
    by_id = {s["id"]: s for s in steps}

    if args.ready:
        ids = dp.ready_steps(steps)
        out = [{
            "id": by_id[i]["id"],
            "step_id": by_id[i]["id"],  # 提醒调度 agent：spawn task 必须带 step_id
            "prompt": by_id[i].get("prompt", ""),
            "agent_name": by_id[i].get("agent_name"),
            "model": by_id[i].get("model"),
            "type": by_id[i].get("type", "generative"),
            "goal": by_id[i].get("goal"),
            "supervisor": by_id[i].get("supervisor"),
        } for i in ids]
        print(json.dumps(out, ensure_ascii=False))

    elif args.status:
        order = dp.topo_order(steps)  # 拓扑序 + 顺带验环
        out = [{
            "id": by_id[i]["id"],
            "name": by_id[i].get("name", ""),
            "status": by_id[i].get("status", "pending"),
            "depends_on": by_id[i].get("depends_on", []),
        } for i in order]
        print(json.dumps(out, ensure_ascii=False))

    elif args.mark_done:
        sid = args.mark_done
        if not dp.mark_done(steps, sid):
            print(f"[dag] unknown step: {sid}", file=sys.stderr)
            sys.exit(1)
        dp.save_dag(ws, dag)
        print(f"[dag] marked done: {sid}")

    elif args.mark_failed:
        sid = args.mark_failed
        if not dp.mark_failed(steps, sid):
            print(f"[dag] unknown step: {sid}", file=sys.stderr)
            sys.exit(1)
        dp.save_dag(ws, dag)
        print(f"[dag] marked failed: {sid}")

    elif args.rerun:
        sid = args.rerun
        if sid not in by_id:
            print(f"[dag] unknown step: {sid}", file=sys.stderr)
            sys.exit(1)
        affected = dp.get_descendants(steps, sid)  # [sid] + 下游，拓扑序
        dp.reset_steps(steps, affected)
        dp.save_dag(ws, dag)
        print(json.dumps(affected, ensure_ascii=False))

    elif args.add_step:
        try:
            new_step = json.loads(args.add_step)
        except json.JSONDecodeError as e:
            print(f"[dag] invalid JSON in --add-step: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(new_step, dict):
            print("[dag] --add-step must be a JSON object", file=sys.stderr)
            sys.exit(1)
        try:
            added = dp.add_step(steps, new_step)
        except ValueError as e:
            print(f"[dag] add-step rejected: {e}", file=sys.stderr)
            sys.exit(1)
        dp.save_dag(ws, dag)
        print(json.dumps(added, ensure_ascii=False))

    elif args.reset_to:
        sid = args.reset_to
        if sid not in by_id:
            print(f"[dag] unknown step: {sid}", file=sys.stderr)
            sys.exit(1)
        affected = dp.get_descendants(steps, sid)  # [sid] + 下游，拓扑序
        dp.reset_steps(steps, affected)
        dp.save_dag(ws, dag)
        print(json.dumps(affected, ensure_ascii=False))


if __name__ == "__main__":
    main()
