"""ExploreAgent — 探索式（禁止 spawn）。"""
from .task import TaskAgent
from ..session.prompt import PromptBuilder


class ExploreAgent(TaskAgent):
    """探索式 — 禁止 spawn。"""

    def build_system_prompt(self) -> str | None:
        return PromptBuilder.build_subagent_system_prompt(
            "explore", self.prompt, self.workspace_path)

    def can_spawn(self) -> bool:
        return False
