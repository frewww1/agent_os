"""Registry — 树状态注册表。

收敛 runs dict + 父子关系 + SpawnRequest 批次的操作。
纯数据结构，不关心进程/IO/持久化/编排。
不定型状态（只存不改语义），不持 backend，不调 bus。
"""
import logging
import re
from collections import defaultdict
from typing import Optional

from .models import RunInfo, RunStatus, SpawnRequest

logger = logging.getLogger("agent_os")


class Registry:
    """agent 运行时注册表：runs + 父子树 + spawn 批次。"""

    def __init__(self):
        self.runs: dict[str, RunInfo] = {}
        self.spawn_requests: dict[str, SpawnRequest] = {}

    # ---- 基本存取 ----

    def register(self, ri: RunInfo) -> None:
        """注册一个 run。"""
        self.runs[ri.run_id] = ri

    def unregister(self, run_id: str) -> Optional[RunInfo]:
        """移除一个 run，返回被移除的对象（不存在则 None）。"""
        return self.runs.pop(run_id, None)

    def get(self, run_id: str) -> Optional[RunInfo]:
        """查询 run，不存在返回 None。"""
        return self.runs.get(run_id)

    # ---- 树查询 ----

    def list_runs(self) -> list[dict]:
        """列出所有 run 的摘要。"""
        result = []
        for ri in self.runs.values():
            result.append({
                "run_id": ri.run_id,
                "prompt": ri.prompt[:100],
                "status": ri.status.value,
                "session_id": ri.session_id,
                "parent_run_id": ri.parent_run_id,
                "children_run_ids": ri.children_run_ids,
                "started_at": ri.started_at.isoformat(),
                "completed_at": ri.completed_at.isoformat() if ri.completed_at else None,
                "events": list(ri.output_events),
                "events_count": len(ri.output_events),
                "turns": len(ri.turn_markers),
            })
        return result

    def get_tree(self) -> list[dict]:
        """返回 agent 树结构（只返回根节点，children 嵌套）。"""
        roots = [ri for ri in self.runs.values() if not ri.parent_run_id]
        return [self._build_tree_node(ri) for ri in roots]

    def _build_tree_node(self, run_info: RunInfo) -> dict:
        """递归构建树节点。"""
        children = []
        for child_id in run_info.children_run_ids:
            child = self.runs.get(child_id)
            if child:
                children.append(self._build_tree_node(child))

        display_prompt = self.unwrap_task_prompt(
            run_info.prompt or "",
            run_info.system_prompt or "",
        )

        return {
            "run_id": run_info.run_id,
            "prompt": display_prompt[:120],
            "goal": run_info.goal or "",
            "goal_retries": run_info.goal_retries,
            "label": run_info.label,
            "status": run_info.status.value,
            "session_id": run_info.session_id,
            "started_at": run_info.started_at.isoformat(),
            "completed_at": run_info.completed_at.isoformat() if run_info.completed_at else None,
            "events": list(run_info.output_events),
            "turns": len(run_info.turn_markers),
            "interactive": run_info.interactive,
            "task_type": run_info.task_type,
            "model": run_info.model,
            "is_root": run_info.parent_run_id is None,
            "workspace_path": run_info.workspace_path,
            "children": children,
        }

    @staticmethod
    def unwrap_task_prompt(prompt: str, system_prompt: str = "") -> str:
        """提取真实任务文本供树视图标题与 resume_parent 子任务摘要共用。

        按优先级依次尝试：
        1. 从 system_prompt 的 ## Task 段提取（新格式：任务指令在 system prompt 中）
        2. 从 prompt 的 [Your Task]...[/Your Task] 提取（旧格式：_wrap_child_prompt 包装）
        3. 剥掉通信协议头后取首行"""
        # 优先从 system_prompt 提取 ## Task 段
        if system_prompt:
            m = re.search(r'## Task\n([\s\S]+?)(?=\n## |\Z)', system_prompt)
            if m:
                task = m.group(1).strip()
                if task:
                    return task
        # 兼容旧格式：[Your Task]...[/Your Task]
        m = re.search(r'\[Your Task\]\n?([\s\S]*?)\n?\[/Your Task\]', prompt)
        if m:
            return m.group(1).strip()
        clean = re.sub(r'\[Agent OS Communication Protocol[\s\S]*?\[/Agent OS Communication Protocol\]\s*', '', prompt)
        clean = re.sub(r'\[Mandatory Closing Step\][\s\S]*?\[/Mandatory Closing Step\]', '', clean).strip()
        return clean.split('\n')[0].strip() or prompt[:80]

    # ---- SpawnRequest 批次 ----

    def get_spawn(self, spawn_id: str) -> Optional[SpawnRequest]:
        """查询 spawn 批次。"""
        return self.spawn_requests.get(spawn_id)

    def link_spawn(
        self,
        spawn_id: str,
        parent_run_id: str,
        parent_session_id: str,
        child_run_ids: list,
        wait_strategy: str = "all",
    ) -> SpawnRequest:
        """注册一个 spawn 批次。"""
        req = SpawnRequest(
            spawn_id=spawn_id,
            parent_run_id=parent_run_id,
            parent_session_id=parent_session_id,
            child_run_ids=child_run_ids,
            wait_strategy=wait_strategy,
        )
        self.spawn_requests[spawn_id] = req
        return req

    def restore_spawn_requests(self) -> None:
        """从已加载的 runs 中重建 spawn_requests。

        通过 parent_run_id 分组：同一个父 agent 的所有子 agent 属于同一个 spawn 请求。
        spawn_id 从父 agent 的 spawn_id 获取。"""
        # 按 parent_run_id 分组子 agent
        parent_groups = defaultdict(list)
        for run_id, ri in self.runs.items():
            if ri.parent_run_id:
                parent_groups[ri.parent_run_id].append(run_id)

        for parent_id, child_ids in parent_groups.items():
            if not child_ids:
                continue
            parent = self.runs.get(parent_id)
            # 过滤掉 supervisor 子 agent（不是 spawn 子 agent）
            active_sup = getattr(parent, '_active_supervisor', None) if parent else None
            child_ids = [cid for cid in child_ids if cid != active_sup]
            if not child_ids:
                continue
            spawn_id = parent.spawn_id if parent else ""
            if not spawn_id:
                # 父 agent 没有 spawn_id（可能是旧数据），生成一个
                spawn_id = f"restored_{parent_id[:8]}"
                if parent:
                    parent.spawn_id = spawn_id

            parent_session = parent.session_id if parent else ""

            # 统计已完成的子 run
            # STOPPED 状态的子 agent：可能在重启前已完成（有结果）或被强制
            # 中止（无结果）。只有有结果的才算"完成"，无结果的视为未完成，
            # 等子 agent 真正完成后由 _on_run_completed 正常触发 resume。
            completed = set()
            for cid in child_ids:
                child = self.runs.get(cid)
                if not child:
                    continue
                if child.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    completed.add(cid)
                elif child.status == RunStatus.STOPPED:
                    # STOPPED 是重启强制改为 stopped 的。
                    # 只有 reported_result（report.py 调用过）才算真正完成。
                    # _fallback_result/messages 可能是上个 session 残留，不可信。
                    if child.reported_result:
                        completed.add(cid)

            spawn_req = SpawnRequest(
                spawn_id=spawn_id,
                parent_run_id=parent_id,
                parent_session_id=parent_session,
                child_run_ids=child_ids,
                wait_strategy="all",
            )
            spawn_req.completed_children = completed
            # 如果所有子 run 都已完成，标记为已解决。
            # 但如果子 agent 仅因重启被强制置为 stopped（无实际结果），
            # 则不标记 resolved —— 等子 agent 真正完成后由 _on_run_completed 触发 resume。
            if len(completed) == len(child_ids):
                child_has_result = lambda cid: (
                    (c := self.runs.get(cid)) and (
                        c.reported_result  # 只有 report.py 调过才算真正完成
                    )
                )
                if any(child_has_result(cid) for cid in child_ids):
                    spawn_req.is_resolved = True
            logger.debug(
                f"restore_spawn_requests: {spawn_id[:10]} parent={parent_id[:8]} "
                f"children={[c[:8] for c in child_ids]} "
                f"completed={[c[:8] for c in completed]} resolved={spawn_req.is_resolved}"
            )
                # else: 子 agent 被强制 stopped 但无结果，不标记 resolved
            self.spawn_requests[spawn_id] = spawn_req

        if parent_groups:
            logger.info(f"restored {len(parent_groups)} spawn requests from historical runs")
