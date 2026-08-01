"""SupervisorAgent — 监督 agent（verdict 驱动完成，路由到父 SupervisorGraph）。"""
import logging
from datetime import datetime

from .base import Agent
from ..models import RunStatus

logger = logging.getLogger("agent_os")


class SupervisorAgent(Agent):
    """监督 agent — verdict 驱动完成，路由到父 SupervisorGraph。"""

    def can_spawn(self) -> bool:
        return False

    def _on_running_exit(self, exit_code: int | None) -> None:
        sup_done = getattr(self, '_supervisor_done', False)
        if sup_done:
            logger.info(f"[{self.run_id[:8]}] Supervisor PASS complete")
            self._transition(RunStatus.COMPLETED)
        else:
            logger.info(f"[{self.run_id[:8]}] Supervisor exited, will be resumed")
        self._ri.completed_at = datetime.now()
        parent_agent = self._get_agent(self.parent_run_id) if self.parent_run_id else None
        if parent_agent:
            parent_agent.on_completed()
        else:
            self.on_completed()

    def on_report(self, result: str) -> bool:
        """supervisor 调 report.py → 路由 verdict 到父 agent。"""
        result = self._sanitize_unicode(result)
        self._ri.reported_result = result
        self._transition(RunStatus.COMPLETED)
        self._ri.completed_at = datetime.now()
        parent_agent = self._get_agent(self.parent_run_id) if self.parent_run_id else None
        if parent_agent:
            parent_agent._route_supervisor_verdict(result)
        else:
            logger.warning(f"[{self.run_id[:8]}] Supervisor report but no parent")
        self._mark_dirty()
        return True

    def on_send(self, msg: str) -> bool:
        """supervisor 调 send.py → 等同 report（verdict）。"""
        return self.on_report(msg)
