"""Agent OS 数据模型：RunStatus、RunInfo、SpawnRequest。

使用 pydantic.BaseModel 替代 dataclass，提供自动序列化/验证/JSON 导出。
"""
from dataclasses import dataclass
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    WAITING = "waiting"       # 主 agent 等待子 agent 完成
    PLAN_PENDING = "plan_pending"  # agent 调用 ExitPlanMode，等待用户审批计划


class RunInfo(BaseModel):
    """单次 claude 执行的状态信息。

    注意：以下划线开头的字段（_session, _reader_thread 等）是运行时字段，
    不参与 pydantic 序列化，通过 object.__setattr__ 直接设置。
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ---- 可序列化字段 ----
    run_id: str
    prompt: str
    status: RunStatus = RunStatus.RUNNING
    interactive: bool = False
    session_id: Optional[str] = None
    model: Optional[str] = None
    task_type: str = "generative"
    reported_result: Optional[str] = None
    user_terminated: bool = False
    messages: list = Field(default_factory=list)
    parent_run_id: Optional[str] = None
    children_run_ids: list = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    workspace_path: Optional[str] = None
    step_id: Optional[str] = None
    system_prompt: Optional[str] = None
    goal: Optional[str] = None
    goal_retries: int = 0
    supervisor: Optional[str] = None  # 监督 Agent 的 system prompt（非空则启用监督模式）
    output_events: deque = Field(default_factory=lambda: deque(maxlen=10000))
    turn_markers: list = Field(default_factory=list)
    spawn_id: Optional[str] = None
    label: Optional[str] = None
    plan_content: Optional[str] = None
    plan_file: Optional[str] = None

    # ---- 运行时字段（非 pydantic，通过 object.__setattr__ 设置） ----
    # 这些在 __init__ 后手动赋值，pydantic 不会覆盖

    def __init__(self, **data):
        super().__init__(**data)
        # 运行时字段默认值
        object.__setattr__(self, '_fallback_result', None)
        object.__setattr__(self, '_recorded', False)
        object.__setattr__(self, '_event_seq', 0)
        object.__setattr__(self, '_session', None)
        object.__setattr__(self, '_reader_thread', None)
        object.__setattr__(self, '_new_output_event', None)
        object.__setattr__(self, '_bus', None)
        # supervisor / goal 运行时字段（原幽灵字段，显式声明）
        object.__setattr__(self, '_active_supervisor', None)
        object.__setattr__(self, '_waiting_supervisor', None)
        object.__setattr__(self, '_max_goal_retries', None)
        object.__setattr__(self, '_supervisor_done', False)
        object.__setattr__(self, '_supervisor_graph_active', False)
        object.__setattr__(self, '_goal_graph_active', False)

    def add_event(self, kind: str, **payload) -> dict:
        """追加结构化事件（前端按 kind 渲染）。通过 EventBus 分发副作用。"""
        self._event_seq += 1
        event = {
            "seq": self._event_seq,
            "ts": datetime.now().isoformat(),
            "kind": kind,
            "turn": max(1, len(self.turn_markers)),
        }
        event.update(payload)
        self.output_events.append(event)
        # 通过 EventBus 分发：唤醒 SSE（run.event）+ 标脏持久化（run.dirty）
        if self._bus:
            self._bus.publish("run.event", run_id=self.run_id, event=event)
            self._bus.publish("run.dirty", run_id=self.run_id)
        return event

    def to_jsonable(self) -> dict:
        """导出为可 JSON 序列化的字典。对话事件从 jsonl 恢复，只存 OS 注入事件。"""
        _OS_KINDS = {"system", "error", "turn", "send", "rewind", "user_done"}
        os_events = [dict(e) for e in self.output_events if e.get("kind") in _OS_KINDS]
        return {
            "run_id": self.run_id,
            "prompt": self.prompt,
            "status": self.status.value,
            "interactive": self.interactive,
            "session_id": self.session_id,
            "model": self.model,
            "task_type": self.task_type,
            "reported_result": self.reported_result,
            "user_terminated": self.user_terminated,
            "workspace_path": self.workspace_path,
            "messages": list(self.messages),
            "fallback_result": self._fallback_result,
            "parent_run_id": self.parent_run_id,
            "children_run_ids": list(self.children_run_ids),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "exit_code": self.exit_code,
            "turn_markers": list(self.turn_markers),
            "spawn_id": self.spawn_id,
            "label": self.label,
            "step_id": self.step_id,
            "system_prompt": self.system_prompt,
            "goal": self.goal,
            "goal_retries": self.goal_retries,
            "plan_content": self.plan_content,
            "plan_file": self.plan_file,
            "supervisor": self.supervisor,
            "waiting_supervisor": getattr(self, '_waiting_supervisor', None),
            "active_supervisor": getattr(self, '_active_supervisor', None),
            "os_events": os_events,
        }


class SpawnRequest(BaseModel):
    """一次 spawn 请求，追踪多个子 agent 的完成状态。"""
    spawn_id: str
    parent_run_id: str
    parent_session_id: str
    child_run_ids: list = Field(default_factory=list)
    wait_strategy: str = "all"  # "all" or "any"
    completed_children: set = Field(default_factory=set)
    is_resolved: bool = False
