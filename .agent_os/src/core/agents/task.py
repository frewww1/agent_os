"""TaskAgent — 生成式子 agent（必须 report.py，有 idle 超时）。"""
import logging
from datetime import datetime

from .base import Agent
from .base import RunStatus
from . import prompts

logger = logging.getLogger("agent_os")


class TaskAgent(Agent):
    """生成式子 agent — 必须 report.py，有 idle 超时。"""

    def build_system_prompt(self) -> str | None:
        return self.system_prompt or _subagent_prompt("generative", self.prompt, self.workspace_path)

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.warning(f"[{self.agent_id[:8]}] Exited without report.py (code={exit_code})")
        self.add_event("error", text="[Agent OS] Exited without report.py — step failed")
        self._transition(RunStatus.FAILED)
        self.completed_at = datetime.now()
        self.on_completed()


def _subagent_prompt(task_type: str, task_prompt: str, workspace_path: str | None) -> str:
    """子 agent 提示词：按类型组合通用块（generative/interactive/explore）。"""
    return prompts.compose(task_type, workspace_path, task_prompt=task_prompt)
