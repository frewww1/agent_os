"""TaskAgent — 生成式子 agent（必须 report.py，有 idle 超时）。"""
import logging
import os
from datetime import datetime

from .base import Agent
from .base import RunStatus

logger = logging.getLogger("agent_os")


class TaskAgent(Agent):
    """生成式子 agent — 必须 report.py，有 idle 超时。"""

    def build_system_prompt(self) -> str | None:
        return _subagent_prompt("generative", self.prompt, self.workspace_path)

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.warning(f"[{self.agent_id[:8]}] Exited without report.py (code={exit_code})")
        self.add_event("error", text="[Agent OS] Exited without report.py — step failed")
        self._transition(RunStatus.FAILED)
        self.completed_at = datetime.now()
        self.on_completed()


def _subagent_prompt(task_type: str, task_prompt: str, workspace_path: str | None) -> str:
    ws_rel = ".agent_os/workspaces/<任务名>/"
    if workspace_path:
        ws_name = os.path.basename(workspace_path.rstrip('/\\'))
        ws_rel = f".agent_os/workspaces/{ws_name}"

    if task_type == "interactive":
        completion = (
            "## How to Complete\n\n"
            "You are an **interactive** agent. The user will click **Done** to complete you.\n"
            "- Do NOT call report.py — it will be ignored.\n\n"
        )
    else:
        completion = (
            "## How to Complete\n\n"
            "When done, call `python .agent_os/report.py --result \"<summary>\"`.\n"
            "- report.py is MANDATORY. Exiting without it = FAILED.\n\n"
        )

    base = (
        "You are a sub-agent under Agent OS.\n\n"
        "## Workspace\n\n"
        f"Shared workspace: {ws_rel}\n\n"
        + completion +
        "## Available Tools\n"
        "- Create sub-agents: use the Task tool\n"
        "- report.py: `python .agent_os/report.py --result \"<summary>\"`\n"
        "- send.py: `python .agent_os/send.py --msg \"<message>\"`\n"
    )
    if task_prompt:
        base += f"\n## Task\n{task_prompt}\n"
    return base
