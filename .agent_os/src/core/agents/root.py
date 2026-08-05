"""RootAgent — 根 agent（无父，auto-complete，无 idle 超时）。"""
import logging
from datetime import datetime

from .base import Agent, RunStatus

logger = logging.getLogger("agent_os")


class RootAgent(Agent):
    """根 agent — auto-complete，无 idle 超时。"""

    @staticmethod
    def _make_system_prompt(workspace_path: str = "") -> str:
        ws = workspace_path or ".agent_os/workspaces/<agent>/"
        return (
            "You are running under Agent OS, a multi-agent orchestration system.\n\n"
            "## Workspace\n\n"
            f"Your workspace is at {ws}\n"
            "The env var $AGENT_OS_WORKSPACE points to this directory.\n\n"
            "## Available Tools\n\n"
            "- Create sub-agents: use the Task tool to spawn child agents\n"
            "- report.py: `python .agent_os/report.py --result \"<summary>\"`\n"
            "- send.py: `python .agent_os/send.py --msg \"<message>\"`\n"
        )

    def build_system_prompt(self) -> str | None:
        return self._make_system_prompt(self.workspace_path)

    def idle_timeout_enabled(self) -> bool:
        return False

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.info(f"[{self.agent_id[:8]}] Root exited (code={exit_code}), auto-complete")
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self.completed_at = datetime.now()
        self.on_completed()
