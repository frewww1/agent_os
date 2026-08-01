"""RootAgent — 根 agent（无父，auto-complete，无 idle 超时）。"""
import logging
from datetime import datetime

from .base import Agent
from ..session.prompt import PromptBuilder
from ..models import RunStatus

logger = logging.getLogger("agent_os")


class RootAgent(Agent):
    """根 agent — auto-complete，无 idle 超时。"""

    def build_system_prompt(self) -> str | None:
        return PromptBuilder.build_root_system_prompt(
            self.workspace_path or ".agent_os/workspaces/<run>/")

    def idle_timeout_enabled(self) -> bool:
        return False

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.info(f"[{self.run_id[:8]}] Root exited (code={exit_code}), auto-complete")
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self._ri.completed_at = datetime.now()
        self.on_completed()
