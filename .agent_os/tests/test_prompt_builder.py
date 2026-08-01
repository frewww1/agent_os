"""PromptBuilder 单元测试 — 可单独运行：pytest tests/test_prompt_builder.py"""
import pytest
from agent_os.src.core.session.prompt import PromptBuilder
from agent_os.src.core.models import RunInfo


class TestPromptBuilder:
    def test_root_system_prompt(self):
        prompt = PromptBuilder.build_root_system_prompt("/ws/test")
        assert "Agent OS" in prompt
        assert "Task tool" in prompt
        assert "report.py" in prompt
        assert "send.py" in prompt
        assert "/ws/test" in prompt

    def test_subagent_generative(self):
        prompt = PromptBuilder.build_subagent_system_prompt("generative", "do something")
        assert "generative" in prompt
        assert "report.py" in prompt
        assert "MANDATORY" in prompt
        assert "do something" in prompt

    def test_subagent_interactive(self):
        prompt = PromptBuilder.build_subagent_system_prompt("interactive", "review me")
        assert "interactive" in prompt
        assert "Do NOT call report.py" in prompt
        assert "review me" in prompt

    def test_generative_vs_interactive_different(self):
        gen = PromptBuilder.build_subagent_system_prompt("generative", "task")
        inter = PromptBuilder.build_subagent_system_prompt("interactive", "task")
        assert gen != inter
        assert "MANDATORY" in gen
        assert "MANDATORY" not in inter

    def test_work_context_with_reported_result(self):
        ri = RunInfo(run_id="r1", prompt="test", session_id="s1")
        ri.reported_result = "task done"
        ctx = PromptBuilder.build_work_context(ri)
        assert "task done" in ctx

    def test_work_context_empty(self):
        ri = RunInfo(run_id="r1", prompt="test", session_id="s1")
        ctx = PromptBuilder.build_work_context(ri)
        assert len(ctx) < 100
