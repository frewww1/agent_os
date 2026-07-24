"""Orchestrator 单元测试 — 可单独运行：pytest tests/test_orchestrator.py

测试 Orchestrator.resolve_process_exit 的各分支 + on_run_completed 骨架。
通过 mock AgentOS 验证定型逻辑，不依赖真实进程。
"""
import pytest
from unittest.mock import MagicMock
from agent_os.src.core.orchestrator import Orchestrator
from agent_os.src.core.registry import Registry
from agent_os.src.core.models import RunInfo, RunStatus
from agent_os.src.core.agent_os import AgentOS


class TestOrchestrator:
    def _make_pm_mock(self):
        pm = MagicMock()
        pm._registry = Registry()
        pm.runs = pm._registry.runs
        pm.spawn_requests = pm._registry.spawn_requests
        pm._mark_dirty = MagicMock()
        pm._on_run_completed = MagicMock()
        pm.recorder = MagicMock()
        pm.MAX_GOAL_RETRIES = 5
        pm._transition = AgentOS._transition.__get__(pm, AgentOS)
        return pm

    def _make_run(self, pm, run_id="r1", status=RunStatus.RUNNING, **kw):
        ri = RunInfo(run_id=run_id, prompt="test", session_id="s1", status=status, **kw)
        pm.runs[run_id] = ri
        return ri

    def test_plan_pending_no_state_change(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.PLAN_PENDING)
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 0)
        assert ri.status == RunStatus.PLAN_PENDING
        pm._mark_dirty.assert_called_once()

    def test_running_interactive_no_state_change(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING, interactive=True)
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 0)
        assert ri.status == RunStatus.RUNNING

    def test_running_reported_result_completes(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING, reported_result="done")
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 0)
        assert ri.status == RunStatus.COMPLETED
        assert ri.completed_at is not None

    def test_running_root_exit_code_nonzero_fails(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING)
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 1)
        assert ri.status == RunStatus.FAILED

    def test_running_root_exit_code_zero_completes(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING)
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 0)
        assert ri.status == RunStatus.COMPLETED

    def test_waiting_all_children_done_completes(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, run_id="p1", status=RunStatus.WAITING)
        ri.children_run_ids = ["c1"]
        self._make_run(pm, run_id="c1", status=RunStatus.COMPLETED, parent_run_id="p1")
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 0)
        assert ri.status == RunStatus.COMPLETED

    def test_waiting_children_still_running_no_change(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, run_id="p1", status=RunStatus.WAITING)
        ri.children_run_ids = ["c1"]
        self._make_run(pm, run_id="c1", status=RunStatus.RUNNING, parent_run_id="p1")
        # 模拟未解决的 spawn 请求
        from agent_os.src.core.models import SpawnRequest
        pm.spawn_requests["sp1"] = SpawnRequest(
            spawn_id="sp1", parent_run_id="p1", parent_session_id="s1",
            child_run_ids=["c1"], wait_strategy="all",
        )
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 0)
        assert ri.status == RunStatus.WAITING

    def test_running_parent_no_report_fails(self):
        pm = self._make_pm_mock()
        ri = self._make_run(pm, status=RunStatus.RUNNING, parent_run_id="p1")
        self._make_run(pm, run_id="p1", status=RunStatus.WAITING)
        orch = Orchestrator(pm)
        orch.resolve_process_exit(ri, 0)
        assert ri.status == RunStatus.FAILED
