"""Agent 生命周期测试 — 可单独运行：pytest tests/test_orchestrator.py

测试 Agent.on_process_exit 各分支。
通过 mock AgentOS 验证定型逻辑，不依赖真实进程。
"""
import pytest
from unittest.mock import MagicMock
from agent_os.src.core.registry import Registry
from agent_os.src.core.models import RunInfo, RunStatus, SpawnRequest
from agent_os.src.core.agent_os import AgentOS
from agent_os.src.core.agents import Agent


class TestAgentExit:
    def _make_pm_mock(self):
        pm = MagicMock()
        pm._registry = Registry()
        pm.runs = pm._registry.runs
        pm.spawn_requests = pm._registry.spawn_requests
        pm._agents = {}
        pm._mark_dirty = MagicMock()
        pm.recorder = MagicMock()
        pm.MAX_GOAL_RETRIES = 5
        pm._transition = AgentOS._transition.__get__(pm, AgentOS)
        pm._get_agent = AgentOS._get_agent.__get__(pm, AgentOS)
        pm.on_run_completed = AgentOS.on_run_completed.__get__(pm, AgentOS)
        pm._sanitize_unicode = lambda x: x
        pm._try_record_step_completion = MagicMock()
        pm._notify_frontend = MagicMock()
        pm._state_dir = "/tmp"
        pm._loop = None
        pm.continue_run = MagicMock(return_value=True)
        return pm

    def _make_run(self, pm, run_id="r1", status=RunStatus.RUNNING, **kw):
        ri = RunInfo(run_id=run_id, prompt="test", session_id="s1", status=status, **kw)
        pm.runs[run_id] = ri
        pm._agents[run_id] = Agent.for_run(ri, pm)
        return ri

    def test_plan_pending_no_state_change(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.PLAN_PENDING)
        pm._agents[ri.run_id].on_process_exit(0)
        assert ri.status == RunStatus.PLAN_PENDING
        pm._mark_dirty.assert_called_once()

    def test_running_interactive_no_state_change(self):
        pm = self._make_pm_mock()
        # interactive agent 必须是子 agent（设计约定：根不可能是 interactive）
        self._make_run(pm, run_id="p1", status=RunStatus.WAITING)
        ri = self._make_run(pm, status=RunStatus.RUNNING, interactive=True, parent_run_id="p1")
        pm._agents[ri.run_id].on_process_exit(0)
        assert ri.status == RunStatus.RUNNING

    def test_running_reported_result_completes(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING, reported_result="done")
        pm._agents[ri.run_id].on_process_exit(0)
        assert ri.status == RunStatus.COMPLETED
        assert ri.completed_at is not None

    def test_running_root_exit_code_nonzero_fails(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING)
        pm._agents[ri.run_id].on_process_exit(1)
        assert ri.status == RunStatus.FAILED

    def test_running_root_exit_code_zero_completes(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING)
        pm._agents[ri.run_id].on_process_exit(0)
        assert ri.status == RunStatus.COMPLETED

    def test_waiting_all_children_done_completes(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, run_id="p1", status=RunStatus.WAITING)
        ri.children_run_ids = ["c1"]
        self._make_run(pm, run_id="c1", status=RunStatus.COMPLETED, parent_run_id="p1")
        pm._agents[ri.run_id].on_process_exit(0)
        assert ri.status == RunStatus.COMPLETED

    def test_waiting_children_still_running_no_change(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, run_id="p1", status=RunStatus.WAITING)
        ri.children_run_ids = ["c1"]
        self._make_run(pm, run_id="c1", status=RunStatus.RUNNING, parent_run_id="p1")
        pm.spawn_requests["sp1"] = SpawnRequest(
            spawn_id="sp1", parent_run_id="p1", parent_session_id="s1",
            child_run_ids=["c1"], wait_strategy="all",
        )
        pm._agents[ri.run_id].on_process_exit(0)
        assert ri.status == RunStatus.WAITING

    def test_running_parent_no_report_fails(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING, parent_run_id="p1")
        self._make_run(pm, run_id="p1", status=RunStatus.WAITING)
        pm._agents[ri.run_id].on_process_exit(0)
        assert ri.status == RunStatus.FAILED
