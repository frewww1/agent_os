"""Agent 生命周期核心测试 — 验证 Agent/AgentOS 状态流转和 spawn 逻辑。"""
import os
import sys
import types
from unittest.mock import MagicMock
import pytest

AGENT_OS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(AGENT_OS_DIR, "src")

if "agent_os" not in sys.modules:
    pkg = types.ModuleType("agent_os")
    pkg.__path__ = [AGENT_OS_DIR]
    pkg.__package__ = "agent_os"
    sys.modules["agent_os"] = pkg
    src_pkg = types.ModuleType("agent_os.src")
    src_pkg.__path__ = [SRC_DIR]
    src_pkg.__package__ = "agent_os.src"
    sys.modules["agent_os.src"] = src_pkg

sys.path.insert(0, AGENT_OS_DIR)

from agent_os.src.core.agents.base import Agent, RunStatus  # noqa: E402


def _make_agent(agent_id, task_type="generative", parent_id=None, interactive=None):
    if interactive is None:
        interactive = (task_type == "interactive")
    backend = MagicMock()
    backend.launch = MagicMock()
    backend.stream = MagicMock(return_value=iter([]))
    backend.get_session_path = MagicMock(return_value=None)
    return Agent(
        backend=backend, project_root=".",
        agent_id=agent_id, prompt=f"test {task_type}",
        task_type=task_type, interactive=interactive,
        parent_id=parent_id,
    )


class TestAgentStatusTransitions:
    def test_valid_transition_running_to_completed(self):
        agent = _make_agent("a1")
        assert agent.status == RunStatus.RUNNING
        agent._transition(RunStatus.COMPLETED)
        assert agent.status == RunStatus.COMPLETED

    def test_valid_transition_running_to_waiting(self):
        agent = _make_agent("a1")
        agent._transition(RunStatus.WAITING)
        assert agent.status == RunStatus.WAITING

    def test_valid_transition_waiting_to_running(self):
        agent = _make_agent("a1")
        agent._transition(RunStatus.WAITING)
        agent._transition(RunStatus.RUNNING)
        assert agent.status == RunStatus.RUNNING

    def test_invalid_transition_noop(self):
        agent = _make_agent("a1")
        agent._transition(RunStatus.COMPLETED)
        # completed -> running 是无效转换，但 _transition 只打 warning，状态被强制设置
        agent._transition(RunStatus.RUNNING)
        assert agent.status == RunStatus.RUNNING

    def test_running_to_stopped(self):
        agent = _make_agent("a1")
        agent._transition(RunStatus.STOPPED)
        assert agent.status == RunStatus.STOPPED


class TestAgentFields:
    def test_agent_has_required_fields(self):
        agent = _make_agent("a1")
        assert agent.agent_id == "a1"
        assert agent.status == RunStatus.RUNNING
        assert agent.task_type == "generative"
        assert agent.children_ids == []
        assert agent.parent_id is None

    def test_interactive_agent(self):
        agent = _make_agent("a2", task_type="interactive")
        assert agent.interactive is True
        assert agent.task_type == "interactive"

    def test_depth_defaults_to_zero(self):
        agent = _make_agent("a1")
        assert agent.depth == 0


class TestParentChild:
    def test_child_has_parent_ref(self):
        parent = _make_agent("parent")
        child = _make_agent("child", parent_id="parent")
        child.parent = parent
        parent.children.append(child)
        assert child.parent is parent
        assert child in parent.children

    def test_children_ids(self):
        parent = _make_agent("parent")
        child1 = _make_agent("child1", parent_id="parent")
        child2 = _make_agent("child2", parent_id="parent")
        child1.parent = parent
        child2.parent = parent
        parent.children = [child1, child2]
        assert set(parent.children_ids) == {"child1", "child2"}


class TestAgentEvents:
    def test_add_event(self):
        agent = _make_agent("a1")
        agent.add_event("text", text="hello")
        assert len(agent.output_events) == 1
        assert agent.output_events[0]["kind"] == "text"
        assert agent.output_events[0]["text"] == "hello"

    def test_add_event_includes_ts(self):
        agent = _make_agent("a1")
        agent.add_event("system", text="test")
        assert "ts" in agent.output_events[0]

    def test_to_jsonable(self):
        agent = _make_agent("a1")
        data = agent.to_jsonable()
        assert data["agent_id"] == "a1"
        assert data["status"] == "running"
        assert data["depth"] == 0


class TestOnCompleted:
    def test_notifies_parent(self):
        parent = _make_agent("parent")
        child = _make_agent("child", parent_id="parent")
        child.parent = parent
        parent.children.append(child)
        parent.notify_child_completed = MagicMock()

        child.reported_result = "done"
        child.on_completed()
        parent.notify_child_completed.assert_called_once_with(child, "done")

    def test_no_parent_no_error(self):
        agent = _make_agent("root")
        agent.on_completed()


class TestCanSpawn:
    def test_generative_can_spawn(self):
        agent = _make_agent("a1", task_type="generative")
        assert agent.can_spawn() is True

    def test_explore_cannot_spawn(self):
        agent = _make_agent("a1", task_type="explore")
        # ExploreAgent 覆写 can_spawn 返回 False
        from agent_os.src.core.agents.explore import ExploreAgent
        assert ExploreAgent.can_spawn(agent) is False

    def test_interactive_can_spawn(self):
        agent = _make_agent("a1", task_type="interactive")
        assert agent.can_spawn() is True
