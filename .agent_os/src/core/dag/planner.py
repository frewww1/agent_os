"""DAG 调度纯函数层：load/save dag.json + 拓扑/就绪/级联/状态 算法。

图算法使用 Python 3.9+ 标准库 graphlib.TopologicalSorter，替代手写 Kahn 实现。

dag.json 结构：
    {"steps": [
        {"id": "research", "name": "调研", "prompt": "...",
         "depends_on": [], "expected_outputs": "...(仅说明，不校验)",
         "agent_name": null, "model": null,
         "status": "pending",          // pending | running | done | failed
         "started_at": "...", "completed_at": "..."}  // ISO 时间戳，状态流转时写入
    ]}
"""
import json
import os
from datetime import datetime
from graphlib import TopologicalSorter, CycleError

DAG_FILENAME = "dag.json"


def _dag_path(workspace_path: str) -> str:
    return os.path.join(os.path.abspath(workspace_path), DAG_FILENAME)


def load_dag(workspace_path: str) -> dict:
    """读取 <workspace>/dag.json；不存在返回 {"steps": []}。"""
    path = _dag_path(workspace_path)
    if not os.path.exists(path):
        return {"steps": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_dag(workspace_path: str, dag: dict) -> None:
    """写回 <workspace>/dag.json。"""
    with open(_dag_path(workspace_path), "w", encoding="utf-8") as f:
        json.dump(dag, f, indent=2, ensure_ascii=False)


def topo_order(steps: list[dict]) -> list[str]:
    """拓扑排序，使用标准库 graphlib.TopologicalSorter。

    depends_on 中不存在的 id 会被过滤（防脏数据）。
    检测环：抛 ValueError。
    """
    id_set = {s["id"] for s in steps}
    if not id_set:
        return []

    # 构建依赖图：{node_id: {deps...}}
    graph = {}
    for s in steps:
        deps = {d for d in s.get("depends_on", []) if d in id_set}
        graph[s["id"]] = deps

    try:
        ts = TopologicalSorter(graph)
        ts.prepare()
        # 手动迭代 get_ready() + done() 获取拓扑序
        # 不能直接用 static_order()，因为 topo_order 可能被多次调用
        # （get_descendants、add_step 都会复用）
        order = []
        while ts.is_active():
            ready = list(ts.get_ready())
            order.extend(ready)
            for node in ready:
                ts.done(node)
    except CycleError:
        raise ValueError(f"DAG has cycles! Unreachable steps detected.")

    if len(order) != len(id_set):
        missing = id_set - set(order)
        raise ValueError(f"DAG has cycles! Unreachable steps: {missing}")
    return order


def ready_steps(steps: list[dict]) -> list[str]:
    """返回所有 ready 的 step id（status==pending 且 depends_on 全部 done），按 topo 序。"""
    by_id = {s["id"]: s for s in steps}
    order = topo_order(steps)  # 复用，顺带验环
    done = {sid for sid, s in by_id.items() if s.get("status") == "done"}
    ready: list[str] = []
    for sid in order:  # 按 topo 序输出，保证稳定
        s = by_id[sid]
        if s.get("status") == "pending" and all(
            d in done for d in s.get("depends_on", [])
        ):
            ready.append(sid)
    return ready


def get_descendants(steps: list[dict], step_id: str) -> list[str]:
    """返回 [step_id] + 所有直接/间接依赖它的下游，按 topo 序（传递闭包）。"""
    # 反向邻接表：dep -> [依赖 dep 的 step]
    children: dict[str, list[str]] = {}
    for s in steps:
        for d in s.get("depends_on", []):
            children.setdefault(d, []).append(s["id"])

    # 反向边 BFS 收集闭包（含起点）
    seen: set[str] = set()
    stack = [step_id]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))

    # 按 topo 序排序输出
    order = topo_order(steps)
    return [sid for sid in order if sid in seen]


# ---- 动态编排：运行时追加 / 重置节点 ----

def add_step(steps: list[dict], step: dict) -> dict:
    """运行时往 DAG 追加一个新节点（动态编排核心）。

    校验：
      - step 必须含非空 id
      - id 不能与已有节点重复
      - depends_on 里的 id 必须都已存在（不允许依赖未来节点）
      - 追加后整图不能成环（复用 topo_order 验环）
    校验失败抛 ValueError；成功则原地 append 到 steps 并返回规范化后的 step。

    新节点默认 status=pending。调度 agent 用此在看到上游产出后动态插入
    下一步（如"已有代码 → 追加测试用例生成节点"）。"""
    sid = step.get("id")
    if not sid:
        raise ValueError("step must have a non-empty 'id'")
    existing = {s["id"] for s in steps}
    if sid in existing:
        raise ValueError(f"step id already exists: {sid}")
    deps = step.get("depends_on", []) or []
    missing = [d for d in deps if d not in existing]
    if missing:
        raise ValueError(f"depends_on references unknown steps: {missing}")

    normalized = {
        "id": sid,
        "name": step.get("name", sid),
        "prompt": step.get("prompt", ""),
        "depends_on": deps,
        "expected_outputs": step.get("expected_outputs", ""),
        "agent_name": step.get("agent_name"),
        "model": step.get("model"),
        "status": "pending",
    }
    candidate = steps + [normalized]
    topo_order(candidate)  # 验环，成环则抛 ValueError，此时不修改 steps
    steps.append(normalized)
    return normalized


def reset_steps(steps: list[dict], ids: list[str]) -> list[str]:
    """把给定 id 批量置 pending 并清除时间戳。返回实际命中的 id 列表。

    用于 git checkout 回退后同步 DAG 状态（回退到某 step 时，该 step
    及其下游都需要重新跑）。"""
    by_id = {s["id"]: s for s in steps}
    hit: list[str] = []
    for sid in ids:
        s = by_id.get(sid)
        if s is None:
            continue
        s["status"] = "pending"
        s.pop("started_at", None)
        s.pop("completed_at", None)
        hit.append(sid)
    return hit


# ---- 状态流转（纯函数，原地改 step dict 的 status + 时间戳） ----

def _find_step(steps: list[dict], step_id: str) -> dict | None:
    """按 id 找到 step dict，找不到返回 None。"""
    for s in steps:
        if s.get("id") == step_id:
            return s
    return None


def mark_running(steps: list[dict], step_id: str) -> bool:
    """把 step 置 running 并记 started_at。返回是否命中该 step。"""
    s = _find_step(steps, step_id)
    if s is None:
        return False
    s["status"] = "running"
    s["started_at"] = datetime.now().isoformat()
    s.pop("completed_at", None)
    return True


def mark_done(steps: list[dict], step_id: str) -> bool:
    """把 step 置 done 并记 completed_at。返回是否命中该 step。"""
    s = _find_step(steps, step_id)
    if s is None:
        return False
    s["status"] = "done"
    s["completed_at"] = datetime.now().isoformat()
    return True


def mark_failed(steps: list[dict], step_id: str) -> bool:
    """把 step 置 failed 并记 completed_at。返回是否命中该 step。"""
    s = _find_step(steps, step_id)
    if s is None:
        return False
    s["status"] = "failed"
    s["completed_at"] = datetime.now().isoformat()
    return True


def resolve_task_type(task: dict, workspace_path: str | None = None) -> str:
    """从 task dict 解析 agent 类型，优先用显式字段，否则从 dag.json 回退。"""
    task_type = task.get("type") or task.get("agent_type") or task.get("subagent_type")
    if task_type:
        return task_type
    step_id = task.get("step_id")
    if step_id and workspace_path:
        try:
            dag = load_dag(workspace_path)
            for s in dag.get("steps", []):
                if s.get("id") == step_id:
                    dag_type = s.get("type", "generative")
                    if dag_type:
                        return dag_type
                    break
        except Exception:
            pass
    return "generative"
