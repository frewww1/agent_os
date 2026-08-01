"""Agent 基类 — agent 生命周期的 OOP 抽象。

类型 hierarchy（5 种，互斥）:
    Agent(ReaderMixin, SpawnMixin, GoalEvalMixin)
    ├── RootAgent          根 agent（无父，auto-complete，无 idle 超时）
    ├── TaskAgent          生成式子 agent（必须 report.py，有 idle 超时）
    │   └── ExploreAgent   探索式（禁止 spawn，否则同 TaskAgent）
    ├── InteractiveAgent   交互式（用户 Done 完成，无 idle 超时）
    └── SupervisorAgent    监督 agent（verdict 驱动，禁止 spawn）

Mixin 组合:
    - ReaderMixin:   进程输出读取（_read_output）
    - SpawnMixin:    spawn children + 完成解析（spawn_children）
    - GoalEvalMixin: goal 评估（_evaluate_goal）

属性访问模式:
    - 读: self.xxx 透明转发到 RunInfo（通过 __getattr__）
    - 写: self._ri.xxx = value 显式写入 RunInfo
"""
import logging
import pathlib
from datetime import datetime
from typing import TYPE_CHECKING

from ..models import RunInfo, RunStatus
from ..session.prompt import PromptBuilder
from ..session.manager import SessionManager
from ..dag import planner as dp
from ._reader import ReaderMixin
from ._spawn import SpawnMixin
from ._goal_eval import GoalEvalMixin

if TYPE_CHECKING:
    from ..agent_os import AgentOS

logger = logging.getLogger("agent_os")


def find_latest_plan_file() -> str | None:
    """返回 ~/.codebuddy/plans/ 下最近修改的 .md 文件路径。"""
    plans_dir = pathlib.Path.home() / ".codebuddy" / "plans"
    if not plans_dir.is_dir():
        return None
    mds = sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(mds[0]) if mds else None


class Agent(ReaderMixin, SpawnMixin, GoalEvalMixin):
    """Agent 生命周期拥有者。基类提供共享逻辑，子类重写类型特定方法。"""

    def __init__(self, ri: RunInfo, os: "AgentOS"):
        self._ri = ri
        self._os = os
        self._session_mgr = SessionManager(os._backend)

    def __getattr__(self, name: str):
        """读转发到 RunInfo（Agent 自身属性由 __getattribute__ 直接找到）。"""
        return getattr(self._ri, name)

    # ----------------------------------------------------------------
    #  基础设施访问（收敛 self._os.* 调用）
    # ----------------------------------------------------------------

    @property
    def _backend(self):
        return self._os._backend

    @property
    def _supervisor_graph(self):
        return self._os._supervisor_graph

    @property
    def _goal_graph(self):
        return self._os._goal_graph

    @property
    def project_root(self) -> str:
        return self._os.project_root

    @property
    def default_model(self) -> str | None:
        return self._os.default_model

    def _transition(self, to_status: RunStatus) -> None:
        self._os._transition(self._ri, to_status)

    def _mark_dirty(self) -> None:
        self._os._mark_dirty()

    def _notify_frontend(self) -> None:
        self._os._notify_frontend(self.run_id)

    def _notify_and_save(self) -> None:
        self._os._notify_and_save(self.run_id)

    def _get_agent(self, run_id: str) -> "Agent | None":
        return self._os._get_agent(run_id)

    @staticmethod
    def _sanitize_unicode(text: str) -> str:
        return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

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
        return bool(getattr(self, '_supervisor_graph_active', False)
                    or getattr(self, '_waiting_supervisor', None))

    def _terminate_process(self, timeout: int = 5) -> None:
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
        self._mark_dirty()

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
            self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
            self._ri.completed_at = datetime.now()
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
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self._ri.completed_at = datetime.now()
        self._try_record_step_completion(self.reported_result or "(无输出)")
        logger.info(f"[{self.run_id[:8]}] Marked {self.status.value}")
        self.on_completed()

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        raise NotImplementedError

    # ----------------------------------------------------------------
    #  report.py / send.py / user Done
    # ----------------------------------------------------------------

    def on_report(self, result: str) -> bool:
        result = self._sanitize_unicode(result)
        self._ri.reported_result = result
        logger.info(f"[{self.run_id[:8]}] report: {result[:50]}")
        self._try_record_step_completion(result)
        if self.status == RunStatus.RUNNING:
            self._terminate_process()
            self._transition(RunStatus.COMPLETED)
            self._ri.completed_at = datetime.now()
            self.add_event("report", text=result)
            self.on_completed()
        elif self.status == RunStatus.COMPLETED:
            self.on_completed()
        self._mark_dirty()
        return True

    def on_send(self, msg: str) -> bool:
        self.messages.append({"time": datetime.now().isoformat(), "msg": msg})
        self.add_event("send", text=msg)
        if self._is_being_supervised():
            self._route_supervisor_verdict(msg)
        return True

    def on_user_done(self) -> None:
        if self.status != RunStatus.RUNNING:
            return
        self._terminate_process(timeout=0)
        self._ri.user_terminated = True
        self._transition(RunStatus.COMPLETED)
        self._ri.completed_at = datetime.now()
        self.add_event("system", text="[Agent OS] Ended by user (Done).")
        self.add_event("user_done")
        self._try_record_step_completion("(用户手动结束)")
        self._notify_frontend()
        self.on_completed()
        self._mark_dirty()

    # ----------------------------------------------------------------
    #  supervisor verdict 路由
    # ----------------------------------------------------------------

    def _route_supervisor_verdict(self, verdict: str) -> None:
        msg_upper = verdict.strip().upper()
        graph_active = getattr(self, '_supervisor_graph_active', False)
        if graph_active:
            if msg_upper.startswith("PASS"):
                self.add_event("system", text="[Agent OS] Supervisor: PASS")
                finished = self._supervisor_graph.resume_supervisor(self.run_id, "PASS")
                if not finished:
                    self._mark_dirty()
                    return
                self._ri.supervisor = None
                self.on_completed()
            elif msg_upper.startswith("CORRECTION"):
                self.add_event("system", text=f"[Agent OS] Supervisor correction: {verdict[:200]}")
                finished = self._supervisor_graph.resume_supervisor(self.run_id, verdict.strip())
                if not finished:
                    self._mark_dirty()
                    return
                self.on_completed()
        else:
            self._ri._waiting_supervisor = None
            if msg_upper.startswith("PASS"):
                self._ri.supervisor = None
                self.add_event("system", text="[Agent OS] Supervisor: PASS")
                self.on_completed()
            elif msg_upper.startswith("CORRECTION"):
                self.add_event("system", text=f"[Agent OS] Supervisor correction: {verdict[:200]}")
                self.resume(verdict.strip(), source="os")
        self._mark_dirty()

    # ----------------------------------------------------------------
    #  完成编排管线
    # ----------------------------------------------------------------

    MAX_GOAL_RETRIES = 5

    def on_completed(self) -> None:
        self._auto_fill_goal()
        for step in (self._supervisor_step, self._goal_step, self._spawn_step):
            if not step():
                return

    def _supervisor_step(self) -> bool:
        if not self._should_run_supervisor():
            return True
        if not self._run_supervisor_cycle():
            return False
        self._ri.supervisor = None
        return True

    def _goal_step(self) -> bool:
        if not self._should_run_goal():
            return True
        if not self._run_goal_cycle():
            return False
        self._ri.goal = None
        return True

    def _spawn_step(self) -> bool:
        self._match_spawn_requests()
        return True

    def _auto_fill_goal(self) -> None:
        if self.goal or not self.step_id or not self.workspace_path:
            return
        try:
            dag = dp.load_dag(self.workspace_path)
            for s in dag.get("steps", []):
                if s.get("id") == self.step_id:
                    goal = s.get("goal") or ""
                    if goal:
                        self._ri.goal = goal
                        logger.info(f"[{self.run_id[:8]}] Goal auto-filled: {goal[:60]}")
                    break
        except Exception as e:
            logger.debug(f"[{self.run_id[:8]}] Goal auto-fill failed: {e}")

    def _should_run_supervisor(self) -> bool:
        return bool(self.supervisor and not self.interactive and self.status == RunStatus.COMPLETED)

    def _run_supervisor_cycle(self) -> bool:
        if not getattr(self, '_supervisor_graph_active', False):
            self._ri._supervisor_graph_active = True
            self.add_event("system", text="[Agent OS] Waiting for supervisor review...")
            return self._supervisor_graph.run(self.run_id)
        return self._supervisor_graph.resume_agent(self.run_id)

    def _should_run_goal(self) -> bool:
        return bool(self.goal and not self.interactive and self.status == RunStatus.COMPLETED)

    def _run_goal_cycle(self) -> bool:
        if not getattr(self, '_goal_graph_active', False):
            self._ri._goal_graph_active = True
            max_retries = getattr(self, '_max_goal_retries', None) or self.MAX_GOAL_RETRIES
            return self._goal_graph.run(self.run_id, self.goal, max_retries)
        return self._goal_graph.resume(self.run_id)

    # ----------------------------------------------------------------
    #  plan / goal / DAG 管理
    # ----------------------------------------------------------------

    def approve_plan(self, feedback: str = "", model: str | None = None) -> bool:
        if self.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() or "Approved. Please proceed."
        self._transition(RunStatus.RUNNING)
        ok = self.resume(msg, source="user", model=model)
        if not ok:
            self._transition(RunStatus.PLAN_PENDING)
        return ok

    def reject_plan(self, feedback: str = "", model: str | None = None) -> bool:
        if self.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() or "Plan rejected. Please revise."
        self._transition(RunStatus.RUNNING)
        return self.resume(msg, source="user", model=model)

    def set_goal(self, goal: str, max_retries: int | None = None) -> bool:
        self._ri.goal = goal
        self._ri.goal_retries = 0
        if max_retries is not None:
            self._ri._max_goal_retries = max_retries
        return True

    def skip_goal(self) -> bool:
        self._ri.goal_retries = getattr(self, '_max_goal_retries', None) or self.MAX_GOAL_RETRIES
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
    #  supervisor 执行
    # ----------------------------------------------------------------

    def _spawn_supervisor(self) -> str:
        """为此 agent 创建监督 agent。"""
        from .supervisor import SupervisorAgent
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
        self._ri._active_supervisor = sup_run_id
        sup_ri = self._os.runs.get(sup_run_id)
        if sup_ri:
            self._os._agents[sup_run_id] = SupervisorAgent(sup_ri, self._os)
        logger.info(f"[{self.run_id[:8]}] Supervisor spawned: {sup_run_id[:8]}")
        return sup_run_id

    # ----------------------------------------------------------------
    #  system prompt / start / resume / stop / 会话
    # ----------------------------------------------------------------

    def build_system_prompt(self) -> str | None:
        return None

    def start(self) -> None:
        pass

    def resume(self, prompt: str, source: str = "user",
               model: str | None = None, goal: str | None = None) -> bool:
        if self._session and self._session.poll() is None:
            if self.status == RunStatus.PLAN_PENDING:
                self._terminate_process()
            else:
                return False
        if not self.session_id:
            return False
        if goal is not None:
            self._ri.goal = goal if goal else None
            self._ri.goal_retries = 0
        elif source != "os":
            self._ri.goal_retries = 0
        prompt = self._sanitize_unicode(prompt)
        effective_model = model if model is not None else (self.model or self.default_model)
        self.turn_markers.append((len(self.output_events), prompt))
        self.add_event("turn", index=len(self.turn_markers))
        self.add_event("prompt", text=prompt, role="user", source=source)
        logger.info(f"[{self.run_id[:8]}] resume: source={source}")
        return self._os._launch_resume(self._ri, prompt, effective_model)

    def stop(self) -> bool:
        if not self._session or self.status != RunStatus.RUNNING:
            return False
        self._terminate_process()
        self._transition(RunStatus.STOPPED)
        self._ri.completed_at = datetime.now()
        self.add_event("system", text="[Agent OS] Stopped by user.")
        self._notify_frontend()
        self._mark_dirty()
        return True

    def rewind_to(self, target_seq: int) -> dict:
        result = self._session_mgr.rewind_to(self._ri, target_seq)
        if result.get("ok"):
            self._transition(RunStatus.STOPPED)
            self._notify_and_save()
        return result

    def clear_context(self) -> dict:
        result = self._session_mgr.clear_context(self._ri)
        if result.get("ok"):
            self._transition(RunStatus.STOPPED)
            self._notify_and_save()
        return result

    # ----------------------------------------------------------------
    #  工厂
    # ----------------------------------------------------------------

    @classmethod
    def for_run(cls, ri: RunInfo, os: "AgentOS") -> "Agent":
        from .supervisor import SupervisorAgent
        from .root import RootAgent
        from .interactive import InteractiveAgent
        from .explore import ExploreAgent
        from .task import TaskAgent

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
