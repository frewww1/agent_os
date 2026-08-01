"""SpawnMixin — spawn children + spawn 解析（从 agents/base.py 拆出）。"""
import asyncio
import json as _json
import logging
import threading
import uuid

from ..models import RunStatus, SpawnRequest
from ..session.prompt import PromptBuilder
from ..dag import planner as dp

logger = logging.getLogger("agent_os")


class SpawnMixin:
    """子 agent 生成 + 完成解析。self = 父 agent。"""

    def spawn_children(self, tasks: list[dict], parent_session_id: str = "",
                       wait_strategy: str = "all") -> dict:
        """批量 spawn 子 agent。self = 父 agent。"""
        if not parent_session_id:
            parent_session_id = self.session_id or ""

        depth = getattr(self, '_depth', 0) or 0
        if depth >= 3:
            logger.warning(f"[{self.run_id[:8]}] spawn depth={depth} >= 3, rejecting")
            return {"spawn_id": "", "child_count": 0, "child_run_ids": [],
                    "error": f"max depth 3 exceeded (depth={depth})"}

        if not self.can_spawn():
            logger.warning(f"[{self.run_id[:8]}] cannot spawn (type={self.task_type})")
            return {"spawn_id": "", "child_count": 0, "child_run_ids": [],
                    "error": f"{self.task_type} agent cannot spawn children"}

        parent_workspace = self.workspace_path
        if not parent_workspace:
            import os as _os
            parent_workspace = _os.path.join(
                _os.path.dirname(_os.path.abspath(__file__)), "workspaces", self.run_id)

        spawn_id = uuid.uuid4().hex[:10]
        child_run_ids = []
        spawned_step_ids = []

        for task in tasks:
            prompt = task.get("prompt", "")
            if not prompt:
                logger.warning(f"spawn: task {task.get('id', '?')} has no prompt, skipping")
                continue
            agent_name = task.get("agent_name")
            task_type = dp.resolve_task_type(task, parent_workspace)
            child_model = task.get("model") or self.model
            step_id = task.get("step_id")
            goal = task.get("goal")
            supervisor = task.get("supervisor")
            if isinstance(supervisor, dict):
                supervisor = _json.dumps(supervisor, ensure_ascii=False)
            if step_id:
                spawned_step_ids.append(step_id)

            sub_system_prompt = PromptBuilder.build_subagent_system_prompt(
                task_type, prompt, parent_workspace)
            task_hint = prompt.split("\n")[0][:80] if prompt else "task"
            wrapped_prompt = f"[Agent OS] Execute: {task_hint}"

            child_env_extras = {}
            if parent_workspace:
                child_env_extras["AGENT_OS_WORKSPACE"] = parent_workspace
            if step_id:
                child_env_extras["AGENT_OS_STEP_ID"] = step_id

            run_id = self._os.start_run(
                prompt=wrapped_prompt, agent_name=agent_name,
                parent_run_id=self.run_id,
                interactive=(task_type == "interactive"),
                system_prompt=sub_system_prompt, model=child_model,
                task_type=task_type, goal=goal, supervisor=supervisor,
                env_extras=child_env_extras if child_env_extras else None,
            )
            child_run_ids.append(run_id)

        # DAG mark_running
        if spawned_step_ids and parent_workspace:
            try:
                dag = dp.load_dag(parent_workspace)
                steps = dag.get("steps", [])
                hit = [sid for sid in spawned_step_ids if dp.mark_running(steps, sid)]
                if hit:
                    dp.save_dag(parent_workspace, dag)
                    logger.info(f"[{self.run_id[:8]}] DAG marked running: {hit}")
            except Exception as e:
                logger.warning(f"[{self.run_id[:8]}] DAG mark_running failed: {e}")

        spawn_req = SpawnRequest(
            spawn_id=spawn_id, parent_run_id=self.run_id,
            parent_session_id=parent_session_id,
            child_run_ids=child_run_ids, wait_strategy=wait_strategy,
        )
        self._os.spawn_requests[spawn_id] = spawn_req
        self._ri.spawn_id = spawn_id

        self._transition(RunStatus.WAITING)
        self._mark_dirty()

        logger.info(f"[{self.run_id[:8]}] spawned {len(child_run_ids)} children, wait={wait_strategy}")
        return {"spawn_id": spawn_id, "child_count": len(child_run_ids),
                "child_run_ids": child_run_ids}

    # ----------------------------------------------------------------
    #  spawn 解析（父 agent 自己处理）
    # ----------------------------------------------------------------

    def _match_spawn_requests(self) -> None:
        """子 agent 完成时调用：标记完成 + 通知父 agent 检查。"""
        for spawn_req in self._os.spawn_requests.values():
            if spawn_req.is_resolved:
                continue
            if self.run_id in spawn_req.child_run_ids:
                spawn_req.completed_children.add(self.run_id)
                parent_agent = self._get_agent(spawn_req.parent_run_id)
                if parent_agent:
                    parent_agent._check_spawn_resolution(spawn_req)

    def _check_spawn_resolution(self, spawn_req: SpawnRequest) -> None:
        """检查 spawn 请求是否满足 resume 条件。self = 父 agent。"""
        if spawn_req.is_resolved:
            return
        if spawn_req.wait_strategy == "all":
            should_resume = all(
                self._os.runs.get(cid) and self._os.runs[cid].status in (
                    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED
                )
                for cid in spawn_req.child_run_ids
            )
        elif spawn_req.wait_strategy == "any":
            should_resume = len(spawn_req.completed_children) > 0
        else:
            should_resume = False

        if should_resume:
            spawn_req.is_resolved = True
            loop = self._os._loop
            if loop and loop.is_running():
                asyncio.ensure_future(self._on_children_resolved_async(spawn_req), loop=loop)
            else:
                threading.Thread(
                    target=self._on_children_resolved, args=(spawn_req,),
                    daemon=True, name=f"resume-{self.run_id[:6]}"
                ).start()

    async def _on_children_resolved_async(self, spawn_req: SpawnRequest) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._on_children_resolved, spawn_req)

    def _on_children_resolved(self, spawn_req: SpawnRequest) -> None:
        """子 agent 全部完成时被调用。self = 父 agent，自己处理 resume。"""
        logger.info(f"[{self.run_id[:8]}] children resolved, resuming")

        resume_prompt = self._build_child_results_summary(spawn_req)

        if self._session and self._session.poll() is None:
            logger.info(f"[{self.run_id[:8]}] parent still running, terminating")
            self._terminate_process()
            self._transition(RunStatus.COMPLETED)

        session_id = self.session_id or spawn_req.parent_session_id
        if not session_id:
            logger.error(f"[{self.run_id[:8]}] no session_id for resume")
            self.add_event("error", text="[Agent OS] Cannot resume - no session_id")
            self._transition(RunStatus.FAILED)
            from datetime import datetime
            self._ri.completed_at = datetime.now()
            return

        self._ri.session_id = session_id
        ok = self.resume(resume_prompt, source="os")
        if not ok:
            logger.error(f"[{self.run_id[:8]}] resume failed")
            self.add_event("error", text="[Agent OS] resume failed")
            self._transition(RunStatus.FAILED)
            from datetime import datetime
            self._ri.completed_at = datetime.now()

    def _build_child_results_summary(self, spawn_req: SpawnRequest) -> str:
        """组装子 agent 结果摘要。self = 父 agent。"""
        parts = ["子 agent 执行完毕，结果如下：", ""]
        for i, child_id in enumerate(spawn_req.child_run_ids, 1):
            child = self._os.runs.get(child_id)
            if not child:
                parts.append(f"子任务 {i}：状态未知\n")
                continue
            task_desc = self._os._registry.unwrap_task_prompt(
                child.prompt, child.system_prompt or "")[:100]
            parts.append(f"子任务 {i}：{task_desc}")
            if child.messages:
                parts.append("过程消息：")
                for m in child.messages:
                    parts.append(f"  - {m['msg']}")
            if child.reported_result:
                tag = "（用户手动结束）" if child.user_terminated else ""
                parts.append(f"最终结果{tag}：{child.reported_result}")
            elif child._fallback_result:
                tag = "（用户手动结束）" if child.user_terminated else ""
                parts.append(f"最终结果{tag}：{child._fallback_result}")
            elif child.user_terminated:
                parts.append("状态：用户手动结束（无输出）")
            else:
                parts.append("最终结果：(未返回结果)")
            parts.append("")
        parts.append("请基于以上结果继续工作。")
        return "\n".join(parts)
