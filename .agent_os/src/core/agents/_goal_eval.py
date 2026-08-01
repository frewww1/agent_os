"""GoalEvalMixin — goal 评估逻辑（从 agents/base.py 拆出）。"""
import logging
import re

from ..session.prompt import PromptBuilder

logger = logging.getLogger("agent_os")


class GoalEvalMixin:
    """goal 评估：启动评估 agent，解析 YES/NO。"""

    def _evaluate_goal(self) -> tuple[bool, str]:
        """评估 goal 是否达成：启动一个评估 agent，解析 YES/NO。"""
        goal = self.goal or ""
        if not goal:
            return True, "no goal"
        context = PromptBuilder.build_work_context(self._ri)
        if not context.strip():
            return True, "no content"

        prompt = (
            f"Evaluate this task outcome. Reply ONLY with YES or NO on the first line, "
            f"then a brief reason on the second line.\n\n"
            f'Goal: {goal}\n\n{context[:12000]}\n\n'
            f'Did the agent achieve the goal? (YES/NO)'
        )
        try:
            handle = self._backend.launch(
                prompt=prompt, model=None,
                system_prompt="You are a concise evaluator. Reply with YES or NO only.",
                cwd=self.project_root,
            )
            stdout_parts = []
            for ev in self._backend.stream(handle):
                text = ev.get("text", "")
                if text:
                    stdout_parts.append(text)
            handle.wait()
            stdout = "".join(stdout_parts).strip()

            if not stdout:
                return True, "eval: empty output (assume met)"

            for line_text in stdout.upper().split("\n"):
                word = line_text.strip().lstrip("-*# ").strip()
                if not word:
                    continue
                if word.startswith("YES"):
                    rest = word[3:].strip()
                    if rest.lower().startswith(("or", "或")):
                        continue
                    return True, stdout[:300]
                if word.startswith("NO"):
                    rest = word[2:].strip()
                    if rest.lower().startswith(("or", "或")):
                        continue
                    return False, stdout[:300]

            if re.search(r'\bYES\b(?!\s*(or|OR|或))', stdout[:500]):
                return True, stdout[:300]
            if re.search(r'\bNO\b(?!\s*(or|OR|或))', stdout[:500]):
                return False, stdout[:300]

            return True, f"eval: unclear (assume met), head={stdout[:150]}"
        except Exception as e:
            logger.warning(f"evaluate failed: {e}")
            return True, f"eval error: {e}"
