"""InteractiveAgent — 交互式（用户 Done 完成，忽略 report.py）。"""
import logging
from datetime import datetime

from .base import Agent
from .task import _subagent_prompt

logger = logging.getLogger("agent_os")


class InteractiveAgent(Agent):
    """交互式 — 用户 Done 完成。"""

    def build_system_prompt(self) -> str | None:
        return _subagent_prompt("interactive", self.prompt, self.workspace_path)

    def idle_timeout_enabled(self) -> bool:
        return False

    def _on_running_exit(self, exit_code: int | None) -> None:
        logger.debug(f"[{self.agent_id[:8]}] Interactive - staying running (waiting for Done)")

    def on_report(self, result: str) -> bool:
        logger.info(f"[{self.agent_id[:8]}] Interactive report.py — ignored")
        return True

    def on_send(self, msg: str) -> bool:
        self.messages.append({"time": datetime.now().isoformat(), "msg": msg})
        self.add_event("send", text=msg)
        return True
