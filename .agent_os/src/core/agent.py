"""Agent — agent 生命周期的 OOP 抽象。

每种 agent 类型拥有自己的 start/resume/complete 行为，
替代散落的 if/elif 类型分叉。

类型 hierarchy（5 种，互斥）:
    Agent
    ├── RootAgent          根 agent（无父，auto-complete，无 idle 超时）
    ├── TaskAgent          生成式子 agent（必须 report.py，有 idle 超时）
    │   └── ExploreAgent   探索式（禁止 spawn，否则同 TaskAgent）
    ├── InteractiveAgent   交互式（用户 Done 完成，无 idle 超时）
    └── SupervisorAgent    监督 agent（verdict 驱动，禁止 spawn）

设计原则:
- Agent 持有 RunInfo 引用（self._ri），通过 __getattr__/__setattr__ 转发字段访问。
  RunInfo 是纯数据（可序列化），Agent 是行为（不序列化，不进持久化层）。
- 类型特定行为通过子类多态实现，消除 if/elif。
- 共享生命周期（start/resume/stop/on_completed）放基类。
- on_completed 管线通过方法链实现，可扩展。
- supervisor verdict 路由统一到 _route_supervisor_verdict。
- spawn 解析由父 agent 自己处理（_on_children_resolved），子 agent 只通知。
"""
import asyncio
import json as _json
import logging
import threading
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from .models import RunInfo, RunStatus, SpawnRequest
from .prompt_builder import PromptBuilder
from . import dag_planner as dp

if TYPE_CHECKING:
    from .agent_os import AgentOS

logger = logging.getLogger("agent_os")


class Agent:
    """Agent 生命周期拥有者。基类提供共享逻辑，子类重写类型特定方法。"""

    _AGENT_OWN = frozenset({'_ri', '_os'})

    def __init__(self, ri: RunInfo, os: "AgentOS"):
        object.__setattr__(self, '_ri', ri)
        object.__setattr__(self, '_os', os)

    def __getattr__(self, name: str):
        """未定义属性转发到 RunInfo（self._ri）。"""
        return getattr(self._ri, name)

    def __setattr__(self, name: str, value):
        """字段写入转发到 RunInfo（_ri/_os 为 Agent 内部属性）。"""
        if name in self._AGENT_OWN:
            object.__setattr__(self, name, value)
        elif name.startswith('_'):
            object.__setattr__(self._ri, name, value)
        else:
            setattr(self._ri, name, value)

    # ----------------------------------------------------------------
    #  共享属性
    # ----------------------------------------------------------------

    @property
    def is_root(self) -> bool:
        return self.parent_run_id is None

    def can_spawn(self) -> bool:
        return True

    def idle_timeout_enabled(self) -> bool:
        return not self.is_root

    def _is_being_supervised(self) -> bool:
        """是否正在被 supervisor 审查。"""
        return bool(getattr(self, '_supervisor_graph_active', False)
                    or getattr(self, '_waiting_supervisor', None))

    def _terminate_process(self, timeout: int = 5) -> None:
        """终止子进程（如果还活着）。"""
        proc = self._session
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                pass

    # ----------------------------------------------------------------
    #  进程退出处理
    # ----------------------------------------------------------------

    def on_process_exit(self, exit_code: int | None) -> None:
        """进程退出后处理。按 status 分发。"""
        if self.status == RunStatus.PLAN_PENDING:
            self._on_plan_pending_exit()
        elif self.status == RunStatus.WAITING:
            self._on_waiting_exit(exit_code)
        elif self.status == RunStatus.RUNNING:
            self._on_running_exit(exit_code)
        else:
            logger.debug(f"[{self.run_id[:8]}] Status {self.status.value}, skip")

    def _on_plan_pending_exit(self) -> None:
        logger.info(f"[{self.run_id[:8]}] plan_pending exit — waiting for approval")
        self._os._mark_dirty()

    def _on_waiting_exit(self, exit_code: int | None) -> None:
        waiting_sup = getattr(self, '_waiting_supervisor', None)
        if waiting_sup and waiting_sup in self._os.runs:
            logger.info(f"[{self.run_id[:8]}] Waiting for supervisor, keep WAITING")
            return
        all_children_done = all(
            not (sr.parent_run_id == self.run_id and not sr.is_resolved)
            for sr in self._os.spawn_requests.values()
        )
        if all_children_done:
            self._os._transition(self._ri, RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
            self.completed_at = datetime.now()
            logger.info(f"[{self.run_id[:8]}] WAITING done, marked {self.status.value}")
            final = self.reported_result or self._fallback_result or "(无输出)"
            self._try_record_step_completion(final)
        else:
            logger.info(f"[{self.run_id[:8]}] WAITING exit, children still running")

    def _on_running_exit(self, exit_code: int | None) -> None:
        if self.reported_result:
            self._finalize_reported(exit_code)
        else:
            self._on_exit_without_report(exit_code)

    def _finalize_reported(self, exit_code: int | None) -> None:
        self._os._transition(self._ri, RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self.completed_at = datetime.now()
        self._try_record_step_completion(self.reported_result or "(无输出)")
        logger.info(f"[{self.run_id[:8]}] Marked {self.status.value}")
        self.on_completed()

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        """无 report.py 退出。子类重写。"""
        raise NotImplementedError

    # ----------------------------------------------------------------
    #  report.py / send.py / user Done
    # ----------------------------------------------------------------

    def on_report(self, result: str) -> bool:
        """agent 调 report.py。基类 = 任务 agent 通用逻辑。"""
        result = self._os._sanitize_unicode(result)
        self.reported_result = result
        logger.info(f"[{self.run_id[:8]}] report: {result[:50]}")
        self._try_record_step_completion(result)

        if self.status == RunStatus.RUNNING:
            self._terminate_process()
            self._os._transition(self._ri, RunStatus.COMPLETED)
            self.completed_at = datetime.now()
            self.add_event("report", text=result)
            self.on_completed()
        elif self.status == RunStatus.COMPLETED:
            self.on_completed()
        self._os._mark_dirty()
        return True

    def on_send(self, msg: str) -> bool:
        """agent 调 send.py。若正在被审查则路由 verdict。"""
        self.messages.append({"time": datetime.now().isoformat(), "msg": msg})
        self.add_event("send", text=msg)
        if self._is_being_supervised():
            self._route_supervisor_verdict(msg)
        return True

    def on_user_done(self) -> None:
        """用户点 Done 强制完成。"""
        if self.status != RunStatus.RUNNING:
            return
        self._terminate_process(timeout=0)
        self.user_terminated = True
        self._os._transition(self._ri, RunStatus.COMPLETED)
        self.completed_at = datetime.now()
        self.add_event("system", text="[Agent OS] Ended by user (Done).")
        self.add_event("user_done")
        self._try_record_step_completion("(用户手动结束)")
        self._os._notify_frontend(self.run_id)
        self.on_completed()
        self._os._mark_dirty()

    # ----------------------------------------------------------------
    #  supervisor verdict 路由（统一入口）
    # ----------------------------------------------------------------

    def _route_supervisor_verdict(self, verdict: str) -> None:
        """处理 supervisor verdict（PASS/CORRECTION）。self = 被审查的 agent。"""
        msg_upper = verdict.strip().upper()
        graph_active = getattr(self, '_supervisor_graph_active', False)

        if graph_active:
            if msg_upper.startswith("PASS"):
                self.add_event("system", text="[Agent OS] Supervisor: PASS")
                finished = self._os._supervisor_graph.resume_supervisor(self.run_id, "PASS")
                if not finished:
                    self._os._mark_dirty()
                    return
                self.supervisor = None
                self.on_completed()
            elif msg_upper.startswith("CORRECTION"):
                self.add_event("system", text=f"[Agent OS] Supervisor correction: {verdict[:200]}")
                finished = self._os._supervisor_graph.resume_supervisor(self.run_id, verdict.strip())
                if not finished:
                    self._os._mark_dirty()
                    return
                self.on_completed()
        else:
            # fallback: _waiting_supervisor（旧逻辑）
            self._waiting_supervisor = None
            if msg_upper.startswith("PASS"):
                self.supervisor = None
                self.add_event("system", text="[Agent OS] Supervisor: PASS")
                self.on_completed()
            elif msg_upper.startswith("CORRECTION"):
                self.add_event("system", text=f"[Agent OS] Supervisor correction: {verdict[:200]}")
                self.resume(verdict.strip(), source="os")
        self._os._mark_dirty()

    # ----------------------------------------------------------------
    #  完成编排管线（方法链，可扩展）
    # ----------------------------------------------------------------

    def on_completed(self) -> None:
        """完成后的编排管线：supervisor → goal → spawn。"""
        self._auto_fill_goal()
        for step in (self._supervisor_step, self._goal_step, self._spawn_step):
            if not step():
                return

    def _supervisor_step(self) -> bool:
        if not self._should_run_supervisor():
            return True
        if not self._run_supervisor_cycle():
            return False
        self.supervisor = None
        return True

    def _goal_step(self) -> bool:
        if not self._should_run_goal():
            return True
        if not self._run_goal_cycle():
            return False
        self.goal = None
        return True

    def _spawn_step(self) -> bool:
        self._match_spawn_requests()
        return True

    # ----------------------------------------------------------------
    #  编排管线 helper
    # ----------------------------------------------------------------

    MAX_GOAL_RETRIES = 5

    def _auto_fill_goal(self) -> None:
        if self.goal or not self.step_id or not self.workspace_path:
            return
        try:
            dag = dp.load_dag(self.workspace_path)
            for s in dag.get("steps", []):
                if s.get("id") == self.step_id:
                    goal = s.get("goal") or ""
                    if goal:
                        self.goal = goal
                        logger.info(f"[{self.run_id[:8]}] Goal auto-filled: {goal[:60]}")
                    break
        except Exception as e:
            logger.debug(f"[{self.run_id[:8]}] Goal auto-fill failed: {e}")

    def _should_run_supervisor(self) -> bool:
        return bool(self.supervisor and not self.interactive and self.status == RunStatus.COMPLETED)

    def _run_supervisor_cycle(self) -> bool:
        if not getattr(self, '_supervisor_graph_active', False):
            self._supervisor_graph_active = True
            self.add_event("system", text="[Agent OS] Waiting for supervisor review...")
            return self._os._supervisor_graph.run(self.run_id)
        return self._os._supervisor_graph.resume_agent(self.run_id)

    def _should_run_goal(self) -> bool:
        return bool(self.goal and not self.interactive and self.status == RunStatus.COMPLETED)

    def _run_goal_cycle(self) -> bool:
        if not getattr(self, '_goal_graph_active', False):
            self._goal_graph_active = True
            max_retries = getattr(self, '_max_goal_retries', None) or self.MAX_GOAL_RETRIES
            return self._os._goal_graph.run(self.run_id, self.goal, max_retries)
        return self._os._goal_graph.resume(self.run_id)

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
                parent_agent = self._os._get_agent(spawn_req.parent_run_id)
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

        # 如果父进程还在跑，先终止
        if self._session and self._session.poll() is None:
            logger.info(f"[{self.run_id[:8]}] parent still running, terminating")
            self._terminate_process()
            self._os._transition(self._ri, RunStatus.COMPLETED)

        session_id = self.session_id or spawn_req.parent_session_id
        if not session_id:
            logger.error(f"[{self.run_id[:8]}] no session_id for resume")
            self.add_event("error", text="[Agent OS] Cannot resume - no session_id")
            self._os._transition(self._ri, RunStatus.FAILED)
            self.completed_at = datetime.now()
            return

        self.session_id = session_id
        ok = self.resume(resume_prompt, source="os")
        if not ok:
            logger.error(f"[{self.run_id[:8]}] resume failed")
            self.add_event("error", text="[Agent OS] resume failed")
            self._os._transition(self._ri, RunStatus.FAILED)
            self.completed_at = datetime.now()

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

    # ----------------------------------------------------------------
    #  plan / goal / DAG 管理
    # ----------------------------------------------------------------

    def approve_plan(self, feedback: str = "", model: str | None = None) -> bool:
        if self.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() or "Approved. Please proceed."
        self._os._transition(self._ri, RunStatus.RUNNING)
        ok = self.resume(msg, source="user", model=model)
        if not ok:
            self._os._transition(self._ri, RunStatus.PLAN_PENDING)
        return ok

    def reject_plan(self, feedback: str = "", model: str | None = None) -> bool:
        if self.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() or "Plan rejected. Please revise."
        self._os._transition(self._ri, RunStatus.RUNNING)
        return self.resume(msg, source="user", model=model)

    def set_goal(self, goal: str, max_retries: int | None = None) -> bool:
        self.goal = goal
        self.goal_retries = 0
        if max_retries is not None:
            self._max_goal_retries = max_retries
        return True

    def skip_goal(self) -> bool:
        self.goal_retries = getattr(self, '_max_goal_retries', self.MAX_GOAL_RETRIES)
        return True

    def _try_record_step_completion(self, result: str) -> None:
        if self.step_id and self.workspace_path:
            try:
                dag = dp.load_dag(self.workspace_path)
                steps = dag.get("steps", [])
                if dp.mark_done(steps, self.step_id):
                    dp.save_dag(self.workspace_path, dag)
                    logger.info(f"[{self.run_id[:8]}] DAG step done: {self.step_id}")
            except Exception as e:
                logger.warning(f"[{self.run_id[:8]}] DAG mark_done failed: {e}")

    # ----------------------------------------------------------------
    #  supervisor / goal 执行
    # ----------------------------------------------------------------

    def _spawn_supervisor(self) -> str:
        """为此 agent 创建监督 agent。"""
        context = PromptBuilder.build_work_context(self._ri)
        if not context.strip():
            return ""
        task_desc = self.goal or self.prompt[:200]
        sup_prompt = (
            f"## 审查任务\n{task_desc}\n\n## Agent 产出\n{context[:8000]}\n\n"
            f"全部满足 → report.py PASS\n有问题 → send.py CORRECTION"
        )
        sup_sys = (
            f"你是严格审查 AI agent 工作的监督者。\n"
            f"验证产出是否满足：\n{self.supervisor}\n\n"
            f"PASS → report.py\nCORRECTION → send.py\nDo NOT report after correction."
        )
        sup_run_id = self._os.start_run(
            prompt=sup_prompt, agent_name=f"supervisor-{self.run_id[:6]}",
            parent_run_id=self.run_id, interactive=False,
            system_prompt=sup_sys, model=self.model, task_type="generative",
            env_extras={"AGENT_OS_PARENT_RUN_ID": self.run_id},
        )
        self._active_supervisor = sup_run_id
        sup_ri = self._os.runs.get(sup_run_id)
        if sup_ri:
            self._os._agents[sup_run_id] = SupervisorAgent(sup_ri, self._os)
        logger.info(f"[{self.run_id[:8]}] Supervisor spawned: {sup_run_id[:8]}")
        return sup_run_id

    def _evaluate_goal(self) -> tuple[bool, str]:
        """评估 goal 是否达成。"""
        goal = self.goal or ""
        if not goal:
            return True, "no goal"
        context = PromptBuilder.build_work_context(self._ri)
        if not context.strip():
            return True, "no content"
        return self._os._backend.evaluate(
            goal=goal, context=context, cwd=self._os.project_root)

    # ----------------------------------------------------------------
    #  system prompt / start / resume / stop
    # ----------------------------------------------------------------

    def build_system_prompt(self) -> str | None:
        """子类重写。"""
        return None

    def start(self) -> None:
        """启动后钩子。基类空操作。"""
        pass

    def resume(self, prompt: str, source: str = "user",
               model: str | None = None, goal: str | None = None) -> bool:
        """恢复会话。"""
        if self._session and self._session.poll() is None:
            if self.status == RunStatus.PLAN_PENDING:
                self._terminate_process()
            else:
                return False
        if not self.session_id:
            return False
        if goal is not None:
            self.goal = goal if goal else None
            self.goal_retries = 0
        elif source != "os":
            self.goal_retries = 0
        prompt = self._os._sanitize_unicode(prompt)
        effective_model = model if model is not None else (self.model or self._os.default_model)
        self.turn_markers.append((len(self.output_events), prompt))
        self.add_event("turn", index=len(self.turn_markers))
        self.add_event("prompt", text=prompt, role="user", source=source)
        logger.info(f"[{self.run_id[:8]}] resume: source={source}")
        return self._os._launch_resume(self._ri, prompt, effective_model)

    def stop(self) -> bool:
        """停止进程。"""
        if not self._session or self.status != RunStatus.RUNNING:
            return False
        self._terminate_process()
        self._os._transition(self._ri, RunStatus.STOPPED)
        self.completed_at = datetime.now()
        self.add_event("system", text="[Agent OS] Stopped by user.")
        self._os._notify_frontend(self.run_id)
        self._os._mark_dirty()
        return True

    # ----------------------------------------------------------------
    #  spawn children（父 agent 行为）
    # ----------------------------------------------------------------

    def spawn_children(self, tasks: list[dict], parent_session_id: str = "",
                       wait_strategy: str = "all") -> dict:
        """批量 spawn 子 agent。self = 父 agent。"""
        os = self._os

        # 补齐 parent_session_id
        if not parent_session_id:
            parent_session_id = self.session_id or ""

        # 深度限制：根(0) → 子(1) → 孙(2)，最多 3 层
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

            run_id = os.start_run(
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

        # 创建 SpawnRequest
        spawn_req = SpawnRequest(
            spawn_id=spawn_id, parent_run_id=self.run_id,
            parent_session_id=parent_session_id,
            child_run_ids=child_run_ids, wait_strategy=wait_strategy,
        )
        os.spawn_requests[spawn_id] = spawn_req
        self.spawn_id = spawn_id

        # 父 agent 转为 WAITING
        os._transition(self._ri, RunStatus.WAITING)
        os._mark_dirty()

        logger.info(f"[{self.run_id[:8]}] spawned {len(child_run_ids)} children, wait={wait_strategy}")
        return {"spawn_id": spawn_id, "child_count": len(child_run_ids),
                "child_run_ids": child_run_ids}

    # ----------------------------------------------------------------
    #  工厂
    # ----------------------------------------------------------------

    @classmethod
    def for_run(cls, ri: RunInfo, os: "AgentOS") -> "Agent":
        if cls._is_supervisor(ri, os):
            return SupervisorAgent(ri, os)
        if ri.parent_run_id is None:
            return RootAgent(ri, os)
        if ri.interactive:
            return InteractiveAgent(ri, os)
        if ri.task_type == "explore":
            return ExploreAgent(ri, os)
        return TaskAgent(ri, os)

    @staticmethod
    def _is_supervisor(ri: RunInfo, os: "AgentOS") -> bool:
        if not ri.parent_run_id:
            return False
        parent = os.runs.get(ri.parent_run_id)
        if not parent:
            return False
        return getattr(parent, '_active_supervisor', None) == ri.run_id


# ====================================================================
#  RootAgent
# ====================================================================

class RootAgent(Agent):
    """根 agent — auto-complete，无 idle 超时。"""

    def build_system_prompt(self) -> str | None:
        return PromptBuilder.build_root_system_prompt(
            self.workspace_path or ".agent_os/workspaces/<run>/")

    def idle_timeout_enabled(self) -> bool:
        return False

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.info(f"[{self.run_id[:8]}] Root exited (code={exit_code}), auto-complete")
        self._os._transition(self._ri, RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self.completed_at = datetime.now()
        self.on_completed()


# ====================================================================
#  TaskAgent
# ====================================================================

class TaskAgent(Agent):
    """生成式子 agent — 必须 report.py，有 idle 超时。"""

    def build_system_prompt(self) -> str | None:
        return PromptBuilder.build_subagent_system_prompt(
            self.task_type, self.prompt, self.workspace_path)

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.warning(f"[{self.run_id[:8]}] Exited without report.py (code={exit_code})")
        self.add_event("error", text="[Agent OS] Exited without report.py — step failed")
        self._os._transition(self._ri, RunStatus.FAILED)
        self.completed_at = datetime.now()
        self.on_completed()


# ====================================================================
#  ExploreAgent
# ====================================================================

class ExploreAgent(TaskAgent):
    """探索式 — 禁止 spawn。"""

    def build_system_prompt(self) -> str | None:
        return PromptBuilder.build_subagent_system_prompt(
            "explore", self.prompt, self.workspace_path)

    def can_spawn(self) -> bool:
        return False


# ====================================================================
#  InteractiveAgent
# ====================================================================

class InteractiveAgent(Agent):
    """交互式 — 用户 Done 完成，忽略 report.py。"""

    def build_system_prompt(self) -> str | None:
        return PromptBuilder.build_subagent_system_prompt(
            "interactive", self.prompt, self.workspace_path)

    def idle_timeout_enabled(self) -> bool:
        return False

    def _on_running_exit(self, exit_code: int | None) -> None:
        logger.debug(f"[{self.run_id[:8]}] Interactive - staying running")

    def on_report(self, result: str) -> bool:
        logger.info(f"[{self.run_id[:8]}] Interactive report.py — ignored")
        return True


# ====================================================================
#  SupervisorAgent
# ====================================================================

class SupervisorAgent(Agent):
    """监督 agent — verdict 驱动完成，路由到父 SupervisorGraph。"""

    def can_spawn(self) -> bool:
        return False

    def _on_running_exit(self, exit_code: int | None) -> None:
        sup_done = getattr(self, '_supervisor_done', False)
        if sup_done:
            logger.info(f"[{self.run_id[:8]}] Supervisor PASS complete")
            self._os._transition(self._ri, RunStatus.COMPLETED)
        else:
            logger.info(f"[{self.run_id[:8]}] Supervisor exited, will be resumed")
        self.completed_at = datetime.now()
        parent_agent = self._os._get_agent(self.parent_run_id) if self.parent_run_id else None
        if parent_agent:
            parent_agent.on_completed()
        else:
            self.on_completed()

    def on_report(self, result: str) -> bool:
        """supervisor 调 report.py → 路由 verdict 到父 agent。"""
        result = self._os._sanitize_unicode(result)
        self.reported_result = result
        self._os._transition(self._ri, RunStatus.COMPLETED)
        self.completed_at = datetime.now()
        parent_agent = self._os._get_agent(self.parent_run_id) if self.parent_run_id else None
        if parent_agent:
            parent_agent._route_supervisor_verdict(result)
        else:
            logger.warning(f"[{self.run_id[:8]}] Supervisor report but no parent")
        self._os._mark_dirty()
        return True

    def on_send(self, msg: str) -> bool:
        """supervisor 调 send.py → 等同 report（verdict）。"""
        return self.on_report(msg)
