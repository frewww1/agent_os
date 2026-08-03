"""GoalAgent — goal 评估 agent。每次 resume 创建新会话，判断 YES/NO。"""
import logging

from .base import Agent
from .base import RunStatus

logger = logging.getLogger("agent_os")


class GoalAgent(Agent):
    """goal 评估 agent — 每次 resume 用新会话，YES/NO 判定。"""

    def can_spawn(self) -> bool:
        return False

    def idle_timeout_enabled(self) -> bool:
        return False

    def _start_new_session(self) -> None:
        import uuid
        self.session_id = str(uuid.uuid4())

    def on_report(self, result: str) -> bool:
        result = self._sanitize_unicode(result)
        self.reported_result = result
        if self.status == RunStatus.RUNNING:
            self._terminate_process()
            self._transition(RunStatus.COMPLETED)
            self.add_event("report", text=result)
            self.on_completed()
        self._dirty = True
        return True

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        fallback = self.fallback_result or ""
        self.reported_result = fallback
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self.on_completed()
