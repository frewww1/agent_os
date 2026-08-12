"""Agent 基类 — agent 生命周期的 OOP 抽象。

类型 hierarchy（6 种，互斥）:
    Agent(BaseModel)
    ├── RootAgent          根 agent（无父，auto-complete，无 idle 超时）
    ├── TaskAgent          生成式子 agent（必须 report.py，有 idle 超时）
    │   └── ExploreAgent   探索式（禁止 spawn，否则同 TaskAgent）
    ├── InteractiveAgent   交互式（用户 Done 完成，无 idle 超时）
    ├── SupervisorAgent    监督 agent（verdict 驱动，禁止 spawn）
    └── GoalAgent          goal 评估 agent（每次 resume 新会话，YES/NO 判定）

Agent 核心接口（子类必须/可覆写）:
    生命周期:  start() / resume() / terminate()
    结束策略:  on_report(result) / on_send(msg) / on_process_exit(code) / on_user_done()
    能力开关:  can_spawn() / idle_timeout_enabled()
    提示词:    build_system_prompt()
    会话管理:  内置 _load_jsonl() / _save_jsonl()
    持久化:    to_jsonable() / add_event()
"""
import json as _json
import logging
import os as _os
import pathlib
import queue
import shutil
import threading
from collections import deque
from datetime import datetime
from typing import ClassVar, Optional

from pydantic import BaseModel, Field, ConfigDict, PrivateAttr

from enum import Enum

from . import prompts


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    WAITING = "waiting"
    PLAN_PENDING = "plan_pending"

logger = logging.getLogger("agent_os")

# OOM 兜底：CLI 进程因内存耗尽（退出码 134 = SIGABRT）崩溃时，
# 自动 --resume 同一会话继续任务。此为该恢复的最大次数。
MAX_OOM_RETRIES = 3


def find_latest_plan_file() -> str | None:
    """返回 ~/.codebuddy/plans/ 下最近修改的 .md 文件路径。"""
    plans_dir = pathlib.Path.home() / ".codebuddy" / "plans"
    if not plans_dir.is_dir():
        return None
    mds = sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(mds[0]) if mds else None


class Agent(BaseModel):
    """Agent = 数据 + 行为。自身就是 pydantic model，自带序列化。

    父子关系：
    - 子 agent 完成时调 notify_child_completed() → 检查 children status 是否全部完成 → resume 父
    - set_goal/set_supervisor 时立即创建子 agent，不延迟
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    # region 可序列化字段
    agent_id: str
    prompt: str
    status: RunStatus = RunStatus.RUNNING
    interactive: bool = False
    session_id: Optional[str] = None
    model: Optional[str] = None
    task_type: str = "generative"
    reported_result: Optional[str] = None
    user_terminated: bool = False
    messages: list = Field(default_factory=list)
    parent_id: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    workspace_path: Optional[str] = None
    step_id: Optional[str] = None
    system_prompt: Optional[str] = None
    goal: Optional[str] = None
    goal_retries: int = 0
    oom_retries: int = 0
    supervisor: Optional[str] = None
    # 已向父 agent 汇总过结果的子 agent id（增量 resume：每次只告知新完成的）
    summarized_children: list[str] = []
    output_events: deque = Field(default_factory=lambda: deque(maxlen=10000))
    turn_markers: list = Field(default_factory=list)
    label: Optional[str] = None
    plan_content: Optional[str] = None
    plan_file: Optional[str] = None
    fallback_result: Optional[str] = None
    max_goal_retries: Optional[int] = None
    depth: int = 0
    _dirty: bool = False
    # endregion

    # region 运行时字段
    def __init__(self, backend=None, project_root: str = ".", **data):
        super().__init__(**data)
        self._backend = backend
        self._project_root = project_root
        self._on_step_done = data.pop("_on_step_done", None)
        self._on_step_start = data.pop("_on_step_start", None)
        self._on_child_created = data.pop("_on_child_created", None)
        self._session = None
        self.parent: Agent | None = None
        self.children: list[Agent] = []
        self._event_queue: queue.Queue = queue.Queue()

    @property
    def children_ids(self) -> list:
        return [c.agent_id for c in self.children]
    # endregion

    # ---- 序列化 ----

    def restore_events(self, os_events: list[dict], cli_events: list[dict] | None = None) -> None:
        """重启后合并 OS 事件 + CLI 事件（由调用方从 jsonl 解析好传入），按 ts 排序。"""
        events: list[dict] = []
        if cli_events:
            for ev in cli_events:
                ev["_src"] = "jsonl"
                events.append(ev)
        for e in os_events:
            e["_src"] = "os"
            events.append(e)
        events.sort(key=lambda e: e.get("ts", ""))
        for e in events:
            self.output_events.append(e)
        if events:
            logger.info(f"[{self.agent_id[:8]}] restored {len(events)} events")

    def add_event(self, kind: str, **payload) -> dict:
        event = {
            "ts": datetime.now().isoformat(),
            "kind": kind,
            "turn": max(1, len(self.turn_markers)),
        }
        event.update(payload)
        self.output_events.append(event)
        # 入队唤醒 SSE
        self._event_queue.put_nowait(event)
        self._dirty = True
        return event

    def to_jsonable(self) -> dict:
        _OS_KINDS = {"system", "error", "turn", "send", "rewind", "user_done"}
        os_events = [dict(e) for e in self.output_events if e.get("kind") in _OS_KINDS]
        return {
            "agent_id": self.agent_id, "prompt": self.prompt,
            "status": self.status.value, "interactive": self.interactive,
            "session_id": self.session_id, "model": self.model,
            "task_type": self.task_type, "reported_result": self.reported_result,
            "user_terminated": self.user_terminated, "workspace_path": self.workspace_path,
            "messages": list(self.messages), "fallback_result": self.fallback_result,
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "exit_code": self.exit_code, "turn_markers": list(self.turn_markers),
            "label": self.label, "step_id": self.step_id,
            "system_prompt": self.system_prompt, "goal": self.goal,
            "goal_retries": self.goal_retries,
            "oom_retries": self.oom_retries,
            "summarized_children": self.summarized_children,
            "max_goal_retries": self.max_goal_retries, "depth": self.depth,
            "plan_content": self.plan_content, "plan_file": self.plan_file,
            "supervisor": self.supervisor,
            "os_events": os_events,
        }

    # ----------------------------------------------------------------
    #  基础设施访问
    # ----------------------------------------------------------------

    @property
    def project_root(self) -> str:
        return self._project_root

    # 合法状态转换表
    _VALID_TRANSITIONS: set[tuple[str, str]] = {
        ("running", "completed"), ("running", "failed"), ("running", "stopped"),
        ("running", "waiting"), ("running", "plan_pending"),
        ("waiting", "running"), ("waiting", "completed"), ("waiting", "failed"),
        ("plan_pending", "running"), ("plan_pending", "stopped"),
        ("stopped", "running"),
        ("completed", "stopped"), ("failed", "stopped"),
    }

    def _transition(self, to_status: RunStatus) -> None:
        if self.status == to_status:
            return
        if (self.status.value, to_status.value) not in self._VALID_TRANSITIONS:
            logger.warning(
                f"[{self.agent_id[:8]}] invalid transition {self.status.value} -> {to_status.value}"
            )
        self.status = to_status
        if to_status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED):
            self.completed_at = datetime.now()
        self._dirty = True

    def _notify_frontend(self) -> None:
        pass

    @staticmethod
    def _sanitize_unicode(text: str) -> str:
        return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    # ----------------------------------------------------------------
    #  视图
    # ----------------------------------------------------------------

    def to_summary(self) -> dict:
        return {
            "agent_id": self.agent_id, "prompt": self.prompt[:100],
            "status": self.status.value, "session_id": self.session_id,
            "parent_id": self.parent_id, "children_ids": self.children_ids,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "events": list(self.output_events),
            "events_count": len(self.output_events),
            "turns": len(self.turn_markers),
        }

    def to_tree_node(self) -> dict:
        children_nodes = [child.to_tree_node() for child in self.children]
        return {
            "agent_id": self.agent_id,
            "prompt": self.prompt[:120] if self.prompt else "",
            "goal": self.goal or "", "goal_retries": self.goal_retries,
            "max_goal_retries": self.max_goal_retries,
            "supervisor": self.supervisor or "",
            "label": self.label, "status": self.status.value,
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "events": list(self.output_events), "turns": len(self.turn_markers),
            "interactive": self.interactive, "task_type": self.task_type,
            "model": self.model, "is_root": self.parent_id is None,
            "step_id": self.step_id,
            "workspace_path": self.workspace_path, "children": children_nodes,
        }

    # ----------------------------------------------------------------
    #  初始化管线
    # ----------------------------------------------------------------

    def initialize(self, prompt: str, model: str | None = None) -> None:
        """Agent 自身的启动管线：turn_marker + start + 初始事件 + launch。"""
        self.turn_markers.append((0, prompt))
        self.start()
        self.add_event("turn", index=1)
        self.add_event("prompt", text=prompt, role="user", source="user")
        self._launch(prompt, model)

    def terminate(self) -> None:
        """终止 agent 进程（暴露给外部调用）。"""
        self._terminate_process()

    # ----------------------------------------------------------------
    #  共享属性
    # ----------------------------------------------------------------

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    def can_spawn(self) -> bool:
        return True

    def idle_timeout_enabled(self) -> bool:
        return not self.is_root

    def _terminate_process(self, timeout: int = 5) -> None:
        proc = self._session
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                pass

    # ----------------------------------------------------------------
    #  子 agent 管理
    # ----------------------------------------------------------------

    def _make_child(self, prompt: str, system_prompt: str, task_type: str = "generative",
                    interactive: bool = False, goal: str | None = None,
                    supervisor: str | None = None, step_id: str | None = None,
                    agent_cls=None) -> "Agent":
        """创建子 agent 并建立父子链接。"""
        import uuid as _uuid
        # 兜底：所有子 agent（含 supervisor/goal）都要知道共享 workspace。
        # 标准模板（prompts.compose）已含 "## Workspace"，此处只补自定义 system_prompt。
        if system_prompt and "## Workspace" not in system_prompt and self.workspace_path:
            system_prompt += "\n\n" + prompts.workspace_block(self.workspace_path)
        child = (agent_cls or self._resolve_child_cls(task_type, interactive))(
                                     backend=self._backend, project_root=self._project_root,
                                     agent_id=_uuid.uuid4().hex[:10], prompt=prompt,
                                     session_id=str(_uuid.uuid4()),
                                     parent_id=self.agent_id,
                                     interactive=interactive, model=self.model,
                                     task_type=task_type, step_id=step_id,
                                     workspace_path=self.workspace_path,
                                     system_prompt=system_prompt,
                                     goal=goal, supervisor=supervisor)
        child.parent = self
        child.depth = (self.depth or 0) + 1
        child._on_step_done = self._on_step_done
        child._on_step_start = self._on_step_start
        child._on_child_created = self._on_child_created
        if self._on_child_created:
            self._on_child_created(child)
        self.children.append(child)
        return child

    @staticmethod
    def _resolve_child_cls(task_type: str, interactive: bool) -> type["Agent"]:
        """按 task_type 选择子 agent 的具体类。

        必须与 _from_serialized 的类型选择一致——否则运行时 spawn 的子
        agent 会退化成 base Agent（_on_exit_without_report 抛异常），
        进程退出后 agent 卡在 running。
        """
        if task_type == "goal":
            from .goal import GoalAgent
            return GoalAgent
        if task_type == "supervisor":
            from .supervisor import SupervisorAgent
            return SupervisorAgent
        if interactive or task_type == "interactive":
            from .interactive import InteractiveAgent
            return InteractiveAgent
        if task_type == "explore":
            from .explore import ExploreAgent
            return ExploreAgent
        from .task import TaskAgent
        return TaskAgent

    def spawn_children(self, tasks: list[dict], parent_session_id: str = "",
                       wait_strategy: str = "all") -> dict:
        """批量 spawn 子 agent。"""
        depth = self.depth or 0
        if depth >= 3:
            return {"child_count": 0, "child_ids": [], "error": f"max depth 3 exceeded"}
        if not self.can_spawn():
            return {"child_count": 0, "child_ids": [], "error": f"{self.task_type} agent cannot spawn"}
        parent_workspace = self.workspace_path
        if not parent_workspace:
            import os as _os
            # 与 build_agent_env 的 workspace 规则一致：project_root/workspaces/<id>
            parent_workspace = _os.path.join(
                self._project_root, "workspaces", self.agent_id)

        child_ids = []
        spawned_step_ids = []
        for task in tasks:
            prompt = task.get("prompt", "")
            if not prompt:
                continue
            task_type = task.get("type") or task.get("agent_type") or task.get("subagent_type") or "generative"
            child_model = task.get("model") or self.model
            step_id = task.get("step_id")
            child_goal = task.get("goal")
            child_supervisor = task.get("supervisor")
            if isinstance(child_supervisor, dict):
                child_supervisor = _json.dumps(child_supervisor, ensure_ascii=False)
            if step_id:
                spawned_step_ids.append(step_id)
            from .task import _subagent_prompt
            sub_sys = _subagent_prompt(task_type, prompt, parent_workspace)
            task_hint = prompt.split("\n")[0][:80] if prompt else "task"
            wrapped = f"[Agent OS] Execute: {task_hint}"
            child = self._make_child(wrapped, sub_sys, task_type,
                                     interactive=(task_type == "interactive"),
                                     goal=child_goal, supervisor=child_supervisor,
                                     step_id=step_id)
            child.initialize(wrapped, child_model)
            child_ids.append(child.agent_id)

        if spawned_step_ids and parent_workspace and self._on_step_start:
            for sid in spawned_step_ids:
                self._on_step_start(parent_workspace, sid)

        self._transition(RunStatus.WAITING)
        self._dirty = True
        logger.info(f"[{self.agent_id[:8]}] spawned {len(child_ids)} children")
        return {"child_count": len(child_ids), "child_ids": child_ids}

    def notify_child_completed(self, child: "Agent", child_result: str | None = None) -> None:
        """子 agent 完成时回调。检查是否所有子 agent 都完成。"""
        from .goal import GoalAgent
        from .supervisor import SupervisorAgent

        # 处理 goal/supervisor agent 的 verdict
        if isinstance(child, GoalAgent):
            self._handle_goal_verdict(child, child_result)
            # goal 评估完成 → 检查是否存在 supervisor，有则 resume 它
            for c in self.children:
                if isinstance(c, SupervisorAgent) and c.status == RunStatus.RUNNING:
                    c.resume("请继续审查。", source="os")
                    return
        elif isinstance(child, SupervisorAgent):
            self._handle_supervisor_verdict(child_result)

        done = sum(1 for c in self.children if c.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED))
        if done < len(self.children):
            logger.info(f"[{self.agent_id[:8]}] child {child.agent_id[:8]} done, "
                        f"{len(self.children) - done} still pending")
            return

        logger.info(f"[{self.agent_id[:8]}] all {len(self.children)} children done")
        if self.parent:
            self.parent.notify_child_completed(self, self.reported_result or self.fallback_result)
        else:
            self._resume_from_children()

    def _handle_goal_verdict(self, goal_agent: "Agent", result: str | None) -> None:
        if not result:
            # 空结果 = goal agent 未能评估，自动跳过（视为通过）
            self.add_event("system", text="[Agent OS] Goal agent produced no verdict, auto-skipping")
            self.goal = None
            self.goal_retries = 0
            return
        is_met = result.strip().upper().startswith("YES")
        if not is_met:
            self.goal_retries += 1
            max_retries = self.max_goal_retries or 5
            if self.goal_retries < max_retries:
                self.add_event("system", text=f"[Agent OS] Goal not met ({self.goal_retries}/{max_retries}), retrying...")
                self._spawn_goal_agent()
                return
        self.goal = None
        self.goal_retries = 0
        tag = "achieved" if is_met else "retries exhausted"
        self.add_event("system", text=f"[Agent OS] Goal {tag}")

    def _handle_supervisor_verdict(self, result: str | None) -> None:
        if not result:
            return
        result_upper = result.strip().upper()
        if result_upper.startswith("PASS"):
            self.add_event("system", text="[Agent OS] Supervisor: PASS")
            self.supervisor = None
        elif result_upper.startswith("CORRECTION"):
            self.add_event("system", text=f"[Agent OS] Supervisor correction: {result[:200]}")
            self.resume(result.strip(), source="os")

    def _resume_from_children(self) -> None:
        if self._session and self._session.poll() is None:
            self._terminate_process()
        if not self.session_id:
            self.add_event("error", text="[Agent OS] Cannot resume - no session_id")
            self._transition(RunStatus.FAILED)
            return
        summary = self._build_child_results_summary()
        self._transition(RunStatus.RUNNING)
        self.resume(summary, source="os")

    def _build_child_results_summary(self) -> str:
        # 增量汇总：只包含本次新完成的子 agent（已汇报过的跳过），
        # 避免 resume 时把历史所有子 agent 的结果全部重复告知父 agent。
        new_children = [c for c in self.children
                        if c.agent_id not in self.summarized_children]
        if not new_children:
            new_children = self.children
        parts = ["子 agent 执行完毕，结果如下：", ""]
        for i, child in enumerate(new_children, 1):
            parts.append(f"子任务 {i}：{child.prompt[:100]}")
            if child.messages:
                parts.append("过程消息：")
                for m in child.messages:
                    parts.append(f"  - {m['msg']}")
            if child.reported_result:
                tag = "（用户手动结束）" if child.user_terminated else ""
                parts.append(f"最终结果{tag}：{child.reported_result}")
            elif child.fallback_result:
                tag = "（用户手动结束）" if child.user_terminated else ""
                parts.append(f"最终结果{tag}：{child.fallback_result}")
            elif child.user_terminated:
                parts.append("状态：用户手动结束（无输出）")
            else:
                parts.append("最终结果：(未返回结果)")
            parts.append("")
        parts.append("请基于以上结果继续工作。")
        # 记录本次已汇总的子 agent（下次 resume 不再重复告知）
        for c in new_children:
            if c.agent_id not in self.summarized_children:
                self.summarized_children.append(c.agent_id)
        return "\n".join(parts)

    # ----------------------------------------------------------------
    #  进程退出处理
    # ----------------------------------------------------------------

    def on_process_exit(self, exit_code: int | None) -> None:
        # agent 正在被删除（delete_agent 已设置 _deleting）→ 忽略退出回调，
        # 防止异步 exit 处理把已删除的 agent 重新持久化/复活
        if getattr(self, "_deleting", False):
            return
        # OOM 兜底：CLI 进程内存耗尽崩溃（code=134）时自动恢复会话
        if self._try_oom_recover(exit_code):
            return
        if self.status == RunStatus.PLAN_PENDING:
            self._on_plan_pending_exit()
        elif self.status == RunStatus.WAITING:
            self._on_waiting_exit(exit_code)
        elif self.status == RunStatus.RUNNING:
            self._on_running_exit(exit_code)

    def _try_oom_recover(self, exit_code: int | None) -> bool:
        """OOM 兜底：CLI 进程因 V8 堆耗尽崩溃（退出码 134 = SIGABRT）时，
        自动 --resume 同一会话继续任务（不截断上下文）。返回 True 表示已触发恢复。"""
        if (exit_code or 0) != 134:
            return False
        if self.user_terminated:
            return False
        if self.status not in (RunStatus.RUNNING, RunStatus.WAITING):
            return False
        if self.oom_retries >= MAX_OOM_RETRIES:
            logger.warning(f"[{self.agent_id[:8]}] OOM recovery exhausted ({self.oom_retries}/{MAX_OOM_RETRIES})")
            return False
        self.oom_retries += 1
        logger.warning(
            f"[{self.agent_id[:8]}] CLI OOM (code=134), auto-resume ({self.oom_retries}/{MAX_OOM_RETRIES})"
        )
        self.add_event("system", text=(
            f"[Agent OS] CLI 进程因内存不足崩溃（exit={exit_code}），"
            f"已自动恢复会话（第 {self.oom_retries}/{MAX_OOM_RETRIES} 次）。请继续完成当前任务。"
        ))
        if not self.resume("继续执行之前的任务，直到完成。", source="os"):
            logger.warning(f"[{self.agent_id[:8]}] OOM auto-resume failed")
            return False
        return True

    def _on_plan_pending_exit(self) -> None:
        self._dirty = True

    def _on_waiting_exit(self, exit_code: int | None) -> None:
        done = sum(1 for c in self.children if c.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED))
        if done < len(self.children):
            logger.info(f"[{self.agent_id[:8]}] WAITING exit, children still running")
            return
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        final = self.reported_result or self.fallback_result or "(无输出)"
        self._try_record_step_completion(final)

    def _on_running_exit(self, exit_code: int | None) -> None:
        if self.reported_result:
            self._finalize_reported(exit_code)
        else:
            self._on_exit_without_report(exit_code)

    def _finalize_reported(self, exit_code: int | None) -> None:
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self._try_record_step_completion(self.reported_result or "(无输出)")
        self.on_completed()

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        """兜底：task_type 未知（base Agent）时进程退出但未 report。

        直接标记 FAILED 而不是 raise NotImplementedError——否则异常会
        冒泡到 reader 线程导致 agent 永远卡在 running。
        """
        logger.warning(f"[{self.agent_id[:8]}] Exited without report (base agent, code={exit_code})")
        self._transition(RunStatus.FAILED)
        self.completed_at = datetime.now()
        self.on_completed()

    # ----------------------------------------------------------------
    #  report.py / send.py / user Done
    # ----------------------------------------------------------------

    def on_report(self, result: str) -> bool:
        result = self._sanitize_unicode(result)
        self.reported_result = result
        self._try_record_step_completion(result)
        if self.status == RunStatus.RUNNING:
            self._terminate_process()
            self._transition(RunStatus.COMPLETED)
            self.add_event("report", text=result)
            self.on_completed()
        elif self.status == RunStatus.COMPLETED:
            self.on_completed()
        self._dirty = True
        return True

    def on_send(self, msg: str) -> bool:
        self.messages.append({"time": datetime.now().isoformat(), "msg": msg})
        self.add_event("send", text=msg)
        if any(isinstance(c, SupervisorAgent) for c in self.children):
            self._handle_supervisor_verdict(msg)
        return True

    def on_user_done(self) -> None:
        if self.status == RunStatus.COMPLETED:
            return
        # interactive agent 被 Stop 后仍可 Done：从 stopped 转 completed 并通知父 agent
        if self.status == RunStatus.RUNNING:
            self._terminate_process(timeout=0)
        self.user_terminated = True
        self._transition(RunStatus.COMPLETED)
        self.completed_at = datetime.now()
        self.add_event("system", text="[Agent OS] Ended by user (Done).")
        self.add_event("user_done")
        self._try_record_step_completion("(用户手动结束)")
        # 通知父 agent（否则 interactive Done 后父 agent 一直等待不 resume）
        self.on_completed()
        self._notify_frontend()
        self.on_completed()
        self._dirty = True

    # ----------------------------------------------------------------
    #  完成管线
    # ----------------------------------------------------------------

    MAX_GOAL_RETRIES: ClassVar[int] = 5

    def _has_active_child(self, cls) -> bool:
        """是否存在活跃（running/waiting）的指定类型子 agent。

        已完成的旧 supervisor/goal 不视为活跃——supervisor 给出 CORRECTION
        后父 agent 修正重跑再次完成时，应重新创建审查 agent 评估新产出。
        """
        return any(isinstance(c, cls) and c.status in (RunStatus.RUNNING, RunStatus.WAITING)
                   for c in self.children)

    def on_completed(self) -> None:
        """完成时检查：有 goal/supervisor 就先评估，没有则通知父 agent。"""
        from .goal import GoalAgent
        from .supervisor import SupervisorAgent

        # 创建 goal / supervisor agent：仅当没有活跃的同类子 agent 时才创建。
        # （旧版判断 not any(isinstance(c, ...)) 会把已完成的旧 supervisor 也算在内，
        #  导致第二次完成时不再重新审查——CORRECTION 修正后无法再次进入监督者。）
        if self.goal and not self._has_active_child(GoalAgent):
            self._spawn_goal_agent()
        if self.supervisor and not self._has_active_child(SupervisorAgent):
            self._spawn_supervisor_agent()

        for child in self.children:
            if isinstance(child, GoalAgent) and child.status == RunStatus.RUNNING:
                child._start_new_session()
                child.resume("请评估。", source="os")
                return
        for child in self.children:
            if isinstance(child, SupervisorAgent) and child.status == RunStatus.RUNNING:
                child.resume("请继续审查。", source="os")
                return
        if self.parent:
            self.parent.notify_child_completed(self, self.reported_result or self.fallback_result)

    def _resume_child(self, child: "Agent") -> None:
        if isinstance(child, GoalAgent):
            child._start_new_session()
        child.resume("请继续。", source="os")

    # ----------------------------------------------------------------
    #  goal 管理
    # ----------------------------------------------------------------

    def set_goal(self, goal: str, max_retries: int | None = None) -> bool:
        """设置 goal，在 agent 完成时自动创建 GoalAgent 评估。"""
        self.goal = goal
        self.goal_retries = 0
        if max_retries is not None:
            self.max_goal_retries = max_retries
        return True

    def _spawn_goal_agent(self) -> None:
        """创建 goal 评估子 agent。"""
        from .goal import GoalAgent
        context = self.build_work_context()
        if not context.strip():
            return
        max_r = self.max_goal_retries or self.MAX_GOAL_RETRIES
        prompt = (
            f"Evaluate this task outcome. Reply ONLY with YES or NO on the first line, "
            f"then a brief reason on the second line.\n\n"
            f'Goal: {self.goal}\n\n{context[:12000]}\n\n'
            f'Did the agent achieve the goal? (YES/NO)'
        )
        sys_prompt = prompts.compose("goal", self.workspace_path)
        child = self._make_child(prompt, sys_prompt, "goal", agent_cls=GoalAgent)
        child.initialize(prompt, self.model)
        logger.info(f"[{self.agent_id[:8]}] Goal agent spawned: {child.agent_id[:8]}")

    def skip_goal(self) -> bool:
        self.goal_retries = self.max_goal_retries or self.MAX_GOAL_RETRIES
        self.goal = None
        return True

    # ----------------------------------------------------------------
    #  supervisor 管理
    # ----------------------------------------------------------------

    def set_supervisor(self, supervisor_prompt: str) -> bool:
        """设置 supervisor，在 agent 完成时自动创建 SupervisorAgent 审查。"""
        self.supervisor = supervisor_prompt
        return True

    def _spawn_supervisor_agent(self) -> None:
        """创建 supervisor 子 agent。"""
        from .supervisor import SupervisorAgent
        context = self.build_work_context()
        if not context.strip():
            return
        task_desc = self.goal or self.prompt[:200]
        sup_prompt = (
            f"## 审查任务\n{task_desc}\n\n## Agent 产出\n{context[:8000]}\n\n"
            f"全部满足 → report.py PASS\n有问题 → send.py CORRECTION"
        )
        sup_sys = prompts.compose(
            "supervisor", self.workspace_path,
            extra=f"验证产出是否满足：\n{self.supervisor}",
        )
        child = self._make_child(sup_prompt, sup_sys, "supervisor", agent_cls=SupervisorAgent)
        child.initialize(sup_prompt, self.model)

    # ----------------------------------------------------------------
    #  plan / DAG 管理
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

    def _try_record_step_completion(self, result: str) -> None:
        if self.step_id and self.workspace_path and self._on_step_done:
            self._on_step_done(self.workspace_path, self.step_id)

    # ----------------------------------------------------------------
    #  进程启动（Agent 自己管理 CLI + Reader）
    # ----------------------------------------------------------------

    def _launch(self, prompt: str, model: str | None = None, resume: bool = False) -> bool:
        """启动 CLI 进程 + Reader 线程。
        Reader 线程通过回调通知 Agent：on_event(ev) 每个事件，on_exit(code) 进程结束。
        """
        import os as _os
        env = _os.environ.copy()
        env["AGENT_OS_AGENT_ID"] = self.agent_id
        if self.workspace_path:
            env["AGENT_OS_WORKSPACE"] = self.workspace_path

        # OOM 防护：长会话 + 大工具结果会撑爆 CLI（Node.js）默认 V8 堆，
        # 注入 --max-old-space-size 抬高堆上限（可用 AGENT_OS_MAX_OLD_SPACE 覆盖，默认 8GB）。
        _oom_mb = _os.environ.get("AGENT_OS_MAX_OLD_SPACE", "8192")
        _opt = f"--max-old-space-size={_oom_mb}"
        _existing = env.get("NODE_OPTIONS", "").strip()
        _parts = [p for p in _existing.split() if p and not p.startswith("--max-old-space-size=")]
        _parts.append(_opt)
        env["NODE_OPTIONS"] = " ".join(_parts)

        cwd = self._project_root
        try:
            # 提示词中的 $AGENT_OS_WORKSPACE 占位符统一替换为绝对路径，
            # 避免 agent 依赖环境变量解析路径。
            ws = self.workspace_path
            if ws and prompt and "$AGENT_OS_WORKSPACE" in prompt:
                prompt = prompt.replace("$AGENT_OS_WORKSPACE", ws)
            sys_prompt = self.build_system_prompt()
            if ws and sys_prompt and "$AGENT_OS_WORKSPACE" in sys_prompt:
                sys_prompt = sys_prompt.replace("$AGENT_OS_WORKSPACE", ws)
            handle = self._backend.launch(
                prompt=prompt, model=model or self.model,
                session_id=None if resume else self.session_id,
                resume_session=self.session_id if resume else None,
                system_prompt=sys_prompt,
                cwd=cwd, env=env,
            )
        except Exception as e:
            logger.error(f"[{self.agent_id[:8]}] Launch failed: {e}")
            self._on_event({"kind": "error", "text": f"[ERROR] Launch failed: {e}"})
            self._on_exit(-1)
            return False

        if resume:
            self._transition(RunStatus.RUNNING)
            self.completed_at = None
            self.exit_code = None

        self._session = handle
        self._start_reader_thread()
        self._dirty = True
        return True

    def _start_reader_thread(self) -> None:
        """启动后台 daemon 线程读取 CLI 输出，通过 _on_event/_on_exit 回调通知。"""
        reader = threading.Thread(
            target=self._read_output, daemon=True,
            name=f"reader-{self.agent_id[:6]}",
        )
        self._reader_thread = reader
        reader.start()

    def _read_output(self) -> None:
        """后台线程：通过 backend.stream() 读取 agent 输出事件。"""
        session = self._session
        if session is None:
            return
        logger.info(f"[{self.agent_id[:8]}] Reader started, pid={session.pid}")
        try:
            for ev in self._backend.stream(session):
                self._on_event(ev)
            session.wait()
            self._on_exit(session.returncode)
        except Exception as e:
            # 预期终止不产生误导性的 [ERROR]：
            # - 进程被替换（resume/切换）导致旧 reader 终止（状态由新进程管理）
            # - 用户停止（状态已由 stop 流程处理）
            # - 当前进程自然退出（interactive 每轮结束等用户输入）→ 仍需走 _on_exit
            if self._is_reader_exit_expected(session):
                current = self._session is session and not self.user_terminated
                if current:
                    # 当前进程已退出：走退出流程更新状态，
                    # 否则 agent 会一直卡在 running
                    try:
                        rc = session.poll()
                    except Exception:
                        rc = -1
                    if rc is None:
                        rc = -1
                    self._on_exit(rc)
                else:
                    logger.debug(f"[{self.agent_id[:8]}] reader stopped (expected): {e}")
                return
            msg = str(e).strip()
            self._on_event({"kind": "error", "text": f"[ERROR] {msg}" if msg else "[ERROR] reader error"})
            self._on_exit(-1)

    def _is_reader_exit_expected(self, session) -> bool:
        """reader 线程异常是否属于预期终止（不应产生 error 事件）。"""
        if self.user_terminated:
            return True
        if self._session is not session:
            return True  # 已切换到新进程，旧 reader 终止是预期的
        try:
            rc = session.poll()
        except Exception:
            rc = None
        return rc is not None  # 进程已退出（自然退出或被终止）

    def _on_event(self, ev: dict) -> None:
        """Reader 线程回调：每个 CLI 事件。"""
        kind = ev.get("kind", "raw")

        if kind == "plan_pending":
            self._transition(RunStatus.PLAN_PENDING)
            plan_file = find_latest_plan_file()
            if plan_file:
                self.plan_file = plan_file
                try:
                    with open(plan_file, encoding="utf-8") as f:
                        self.plan_content = f.read()
                except Exception:
                    pass
            if self._session:
                self._session.terminate()
        elif kind == "system":
            sid = ev.get("session_id", "")
            if sid and self.session_id and sid != self.session_id:
                self.session_id = sid
        elif kind == "result":
            result_text = ev.get("result", "")
            if result_text and not self.reported_result:
                self.fallback_result = result_text

        payload = {k: v for k, v in ev.items() if k != "kind"}
        if kind == "plan_pending":
            payload["agent_id"] = self.agent_id
        self.add_event(kind, **payload)

    def _on_exit(self, exit_code: int | None) -> None:
        """Reader 线程回调：进程结束。

        幂等：只处理一次。若 _on_exit 内部（如状态转换）抛异常，
        会冒泡到 reader 的 except 分支并再次进入此处，必须防重，
        否则可能递归调用导致 reader 崩溃、agent 卡在 running。
        """
        if getattr(self, "_exit_fired", False):
            return
        self._exit_fired = True
        self.exit_code = exit_code
        logger.info(f"[{self.agent_id[:8]}] Session ended: code={exit_code}")
        self.on_process_exit(exit_code)

    # ----------------------------------------------------------------
    #  system prompt / start / resume / stop / 会话
    # ----------------------------------------------------------------

    def build_system_prompt(self) -> str | None:
        return self.system_prompt

    def build_work_context(self) -> str:
        """构建当前 agent 的工作产出上下文（供 goal/supervisor 子 agent 使用）。"""
        parts = []
        if self.reported_result:
            parts.append(f"Final report: {self.reported_result}")
        elif self.fallback_result:
            parts.append(f"Final output: {self.fallback_result}")
        log_lines = []
        delta_buf = []
        for e in self.output_events:
            kind = e.get("kind", "")
            if kind == "text_delta":
                delta_buf.append(e.get("text", ""))
            elif kind in ("text", "tool_result", "report"):
                if delta_buf:
                    log_lines.append("".join(delta_buf))
                    delta_buf = []
                log_lines.append(e.get("text", ""))
        if delta_buf:
            log_lines.append("".join(delta_buf))
        if log_lines:
            parts.append("Work log:\n" + "\n".join(log_lines))
        if self.messages:
            msgs = "\n".join(m.get("msg", "") for m in self.messages[-15:])
            if msgs.strip():
                parts.append(f"Progress messages: {msgs}")
        return "\n\n".join(parts)[:12000]

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
            self.goal = goal if goal else None
            self.goal_retries = 0
        elif source != "os":
            self.goal_retries = 0
        prompt = self._sanitize_unicode(prompt)
        effective_model = model if model is not None else self.model
        self.turn_markers.append((len(self.output_events), prompt))
        self.add_event("turn", index=len(self.turn_markers))
        self.add_event("prompt", text=prompt, role="user", source=source)
        logger.info(f"[{self.agent_id[:8]}] resume: source={source}")
        return self._launch(prompt, effective_model, resume=True)

    def stop(self) -> bool:
        if not self._session or self.status != RunStatus.RUNNING:
            return False
        self._terminate_process()
        self._transition(RunStatus.STOPPED)
        self.add_event("system", text="[Agent OS] Stopped by user.")
        self._notify_frontend()
        self._dirty = True
        return True

    def rewind_to(self, target_ts: str) -> dict:
        """回退会话到 ts=target_ts 的 user prompt 之前。"""
        if self.status in (RunStatus.RUNNING, RunStatus.WAITING):
            return {"ok": False, "error": f"cannot rewind while status={self.status.value}"}
        if not self.session_id:
            return {"ok": False, "error": "no session_id"}

        # 找目标事件
        target_ev = None
        for ev in self.output_events:
            if ev.get("ts") == target_ts:
                if ev.get("kind") != "prompt" or ev.get("source") != "user":
                    return {"ok": False, "error": "target event is not a user prompt"}
                target_ev = ev
                break
        if not target_ev:
            return {"ok": False, "error": f"event ts={target_ts} not found"}

        # 找 jsonl 路径
        cwd = self.workspace_path or _os.getcwd()
        jsonl_path = self._backend.get_session_path(self.session_id, cwd)
        if not jsonl_path:
            return {"ok": False, "error": "session jsonl not found"}

        # 截断 jsonl
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            return {"ok": False, "error": f"read jsonl failed: {e}"}

        target_text = target_ev.get("text", "")
        target_ts_ms = None
        try:
            target_ts_ms = int(datetime.fromisoformat(target_ev["ts"]).timestamp() * 1000)
        except Exception:
            pass

        cut_line_idx = None
        for idx, raw in enumerate(lines):
            try:
                obj = _json.loads(raw)
            except Exception:
                continue
            if obj.get("role") != "user" or obj.get("type") != "message":
                continue
            content = obj.get("content") or []
            if not content or not isinstance(content, list):
                continue
            first = content[0]
            if not isinstance(first, dict) or first.get("type") != "input_text":
                continue
            if first.get("text", "") != target_text:
                continue
            if target_ts_ms is not None:
                ts = obj.get("timestamp")
                if isinstance(ts, (int, float)) and abs(ts - target_ts_ms) > 60_000:
                    continue
            cut_line_idx = idx

        if cut_line_idx is None:
            return {"ok": False, "error": "could not locate target prompt in jsonl"}

        backup = jsonl_path + f".rewind-{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        try:
            with open(backup, "w", encoding="utf-8") as f:
                f.writelines(lines)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.writelines(lines[:cut_line_idx])
        except Exception as e:
            return {"ok": False, "error": f"truncate jsonl failed: {e}"}

        self._truncate_memory(target_ev)
        self.add_event("rewind", ts=target_ts, jsonl_cut_line=cut_line_idx)
        self._transition(RunStatus.STOPPED)
        self._notify_frontend()
        self._dirty = True
        return {"ok": True, "ts": target_ts, "jsonl_cut_line": cut_line_idx, "backup": backup}

    def clear_context(self) -> dict:
        """清空对话上下文。"""
        if self.status in (RunStatus.RUNNING, RunStatus.WAITING):
            return {"ok": False, "error": f"cannot clear while status={self.status.value}"}
        if not self.session_id:
            return {"ok": False, "error": "no session_id"}

        cwd = self.workspace_path or _os.getcwd()
        jsonl_path = self._backend.get_session_path(self.session_id, cwd)
        if not jsonl_path:
            return {"ok": False, "error": "session jsonl not found"}

        backup = jsonl_path + f".clear-{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        try:
            shutil.copy2(jsonl_path, backup)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.write("")
        except Exception as e:
            return {"ok": False, "error": f"clear jsonl failed: {e}"}

        self._clear_memory()
        self.add_event("system", text="Context cleared — ready for new input")
        self._transition(RunStatus.STOPPED)
        self._notify_frontend()
        self._dirty = True
        return {"ok": True, "backup": backup}

    def _truncate_memory(self, target_ev: dict) -> None:
        """截断内存中的事件、turn_markers、状态（rewind 用）。"""
        target_ts = target_ev.get("ts", "")
        kept = [e for e in self.output_events if e.get("ts", "") < target_ts]
        self.output_events.clear()
        for e in kept:
            self.output_events.append(e)
        target_text = target_ev.get("text", "")
        new_markers = []
        for m in self.turn_markers:
            if isinstance(m, (list, tuple)) and len(m) >= 2 and m[1] == target_text:
                break
            new_markers.append(m)
        self.turn_markers = new_markers
        self.reported_result = None
        self.fallback_result = None
        self.user_terminated = False
        self.exit_code = None
        self.completed_at = None

    def _clear_memory(self) -> None:
        """清空内存中的事件、turn_markers、状态（clear_context 用）。"""
        self.output_events.clear()
        self.turn_markers.clear()
        self.messages.clear()
        self.reported_result = None
        self.fallback_result = None
        self.user_terminated = False
        self.exit_code = None
        self.completed_at = None

    # ----------------------------------------------------------------
    #  工厂
    # ----------------------------------------------------------------

    @classmethod
    def for_run(cls, backend=None, project_root=".", **fields) -> "Agent":
        from .goal import GoalAgent
        from .root import RootAgent
        from .interactive import InteractiveAgent
        from .explore import ExploreAgent
        from .task import TaskAgent

        task_type = fields.get("task_type", "generative")
        kwargs = dict(backend=backend, project_root=project_root, **fields)
        if task_type == "goal":
            return GoalAgent(**kwargs)
        if task_type == "supervisor":
            from .supervisor import SupervisorAgent
            return SupervisorAgent(**kwargs)
        if fields.get("parent_id") is None:
            return RootAgent(**kwargs)
        if fields.get("interactive"):
            return InteractiveAgent(**kwargs)
        if task_type == "explore":
            return ExploreAgent(**kwargs)
        return TaskAgent(**kwargs)
