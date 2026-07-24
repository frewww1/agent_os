"""SupervisorGraph 单元测试 — 可单独运行：pytest tests/test_supervisor_graph.py

测试 SupervisorGraph 的 graph 结构 + 核心节点逻辑。
SupervisorGraph 当前为实验性骨架，未集成到 Orchestrator。
"""
import pytest
import tempfile
from unittest.mock import MagicMock
from agent_os.src.core.supervisor_graph import SupervisorGraph, SupervisorState


class TestSupervisorGraph:
    def _make_pm(self):
        pm = MagicMock()
        pm._state_dir = tempfile.gettempdir()
        pm._spawn_supervisor = MagicMock(return_value="sup1")
        pm._build_work_context = MagicMock(return_value="agent output")
        pm.continue_run = MagicMock()
        pm.runs = {"r1": MagicMock()}
        return pm

    def test_graph_creation(self):
        """SupervisorGraph 能正常创建。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        assert sg._graph is not None

    def test_spawn_supervisor_node(self):
        """_spawn_supervisor 节点调 pm._spawn_supervisor。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        state = SupervisorState(
            agent_run_id="r1", supervisor_run_id="",
            verdict=None, correction_feedback="", review_round=0,
        )
        result = sg._spawn_supervisor(state)
        assert result["supervisor_run_id"] == "sup1"
        pm._spawn_supervisor.assert_called_once()

    def test_route_pass_returns_done(self):
        """_route: verdict=PASS → done。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        state = SupervisorState(
            agent_run_id="r1", supervisor_run_id="sup1",
            verdict="PASS", correction_feedback="", review_round=0,
        )
        assert sg._route(state) == "done"

    def test_route_correction_returns_correct(self):
        """_route: verdict=CORRECTION → correct。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        state = SupervisorState(
            agent_run_id="r1", supervisor_run_id="sup1",
            verdict="CORRECTION", correction_feedback="fix it", review_round=0,
        )
        assert sg._route(state) == "correct"

    def test_route_none_returns_correct(self):
        """_route: verdict=None → correct（默认走审查）。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        state = SupervisorState(
            agent_run_id="r1", supervisor_run_id="sup1",
            verdict=None, correction_feedback="", review_round=0,
        )
        assert sg._route(state) == "correct"

    def test_run_pass_first_try(self):
        """首次创建 supervisor → interrupt 等 verdict → run 返回 False（暂停）。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        finished = sg.run("r1")
        # interrupt 暂停 → run 返回 False（未完成，等 verdict）
        assert finished is False
        pm._spawn_supervisor.assert_called_once()

    def test_resume_supervisor_pass(self):
        """resume_supervisor 传 PASS → graph 完成。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        # 先 run（创建 supervisor + interrupt）
        sg.run("r1")
        # resume with PASS
        finished = sg.resume_supervisor("r1", "PASS")
        assert finished is True
