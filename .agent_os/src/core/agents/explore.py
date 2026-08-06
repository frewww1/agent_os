"""ExploreAgent — 探索式（禁止 spawn）。"""
from .task import TaskAgent, _subagent_prompt


class ExploreAgent(TaskAgent):
    """探索式 — 禁止 spawn，其余同 TaskAgent。"""

    def build_system_prompt(self) -> str | None:
        return self.system_prompt or _subagent_prompt("explore", self.prompt, self.workspace_path)

    def can_spawn(self) -> bool:
        return False
