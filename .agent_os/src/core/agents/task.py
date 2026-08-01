"""TaskAgent — 生成式子 agent（必须 report.py，有 idle 超时）。"""
import logging
from datetime import datetime

from .base import Agent
from ..session.prompt import PromptBuilder
from ..models import RunStatus

logger = logging.getLogger("agent_os")


class TaskAgent(Agent):
    """生成式子 agent — 必须 report.py，有 idle 超时。"""

    def build_system_prompt(self) -> str | None:
        return PromptBuilder.build_subagent_system_prompt(
            self.task_type, self.prompt, self.workspace_path)

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.warning(f"[{self.run_id[:8]}] Exited without report.py (code={exit_code})")
        self.add_event("error", text="[Agent OS] Exited without report.py — step failed")
        self._transition(RunStatus.FAILED)
        self._ri.completed_at = datetime.now()
        self.on_completed()
