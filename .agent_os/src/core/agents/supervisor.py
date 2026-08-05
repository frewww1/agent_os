"""SupervisorAgent — 监督 agent（verdict 驱动完成）。"""
import logging
from datetime import datetime

from .base import Agent
from .base import RunStatus

logger = logging.getLogger("agent_os")


class SupervisorAgent(Agent):
    """监督 agent — verdict 驱动完成。

    结束策略:
    - on_report: 路由 verdict（PASS/CORRECTION）到父 agent
    - on_send:   等同 report
    - on_process_exit: 检查是否有结果
    - on_user_done: 基类默认
    """

    def can_spawn(self) -> bool:
        return False

    def idle_timeout_enabled(self) -> bool:
        return False

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        fallback = self.fallback_result or ""
        if fallback:
            self.reported_result = fallback
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self.completed_at = datetime.now()
        if self.parent:
            self.parent.notify_child_completed(self, self.reported_result or fallback)

    def on_report(self, result: str) -> bool:
        result = self._sanitize_unicode(result)
        self.reported_result = result
        self._transition(RunStatus.COMPLETED)
        self.completed_at = datetime.now()
        if self.parent:
            self.parent.notify_child_completed(self, result)
        self._dirty = True
        return True

    def on_send(self, msg: str) -> bool:
        return self.on_report(msg)
