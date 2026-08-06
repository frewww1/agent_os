"""RootAgent — 根 agent（无父，auto-complete，无 idle 超时）。"""
import logging
from datetime import datetime

from .base import Agent, RunStatus
from . import prompts

logger = logging.getLogger("agent_os")


class RootAgent(Agent):
    """根 agent — auto-complete，无 idle 超时。"""

    @staticmethod
    def _make_system_prompt(workspace_path: str = "", dag_steps: list | None = None) -> str:
        """根 agent 提示词：通用块组合（workspace + spawn + report/send，可选 DAG）。"""
        return prompts.compose("root", workspace_path, dag_steps=dag_steps)

    def build_system_prompt(self) -> str | None:
        # 已设置（含用户自定义）优先；为空时用当前 workspace 动态生成
        return self.system_prompt or self._make_system_prompt(self.workspace_path)

    def idle_timeout_enabled(self) -> bool:
        return False

    def _on_exit_without_report(self, exit_code: int | None) -> None:
        logger.info(f"[{self.agent_id[:8]}] Root exited (code={exit_code}), auto-complete")
        self._transition(RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        self.completed_at = datetime.now()
        self.on_completed()
