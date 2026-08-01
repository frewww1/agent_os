"""SupervisorGraph 单元测试 — 可单独运行：pytest tests/test_supervisor_graph.py

测试 SupervisorGraph 的 graph 结构 + 核心节点逻辑。
SupervisorGraph 当前为实验性骨架，未集成到 Orchestrator。
"""
import pytest
import tempfile
from unittest.mock import MagicMock
from agent_os.src.core.graph.supervisor import SupervisorGraph, SupervisorState


class TestSupervisorGraph:
    def _make_pm(self):
        pm = MagicMock()
        pm._state_dir = tempfile.gettempdir()
        pm.continue_run = MagicMock()
        ri = MagicMock()
        ri.run_id = "r1"
        ri.supervisor = "check"
        ri.goal = "goal"
        ri.prompt = "prompt"
        ri.reported_result = "result"
        ri._fallback_result = None
        ri.user_terminated = False
        ri.messages = []
        pm.runs = {"r1": ri}
        # _get_agent 返回 mock agent，_spawn_supervisor 在 agent 上
        agent = MagicMock()
        agent._spawn_supervisor = MagicMock(return_value="sup1")
        pm._get_agent = MagicMock(return_value=agent)
        pm._mock_agent = agent  # 供测试断言
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
        pm._mock_agent._spawn_supervisor.assert_called_once()

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
        pm._mock_agent._spawn_supervisor.assert_called_once()

    def test_resume_supervisor_pass(self):
        """resume_supervisor 传 PASS → graph 完成。"""
        pm = self._make_pm()
        sg = SupervisorGraph(pm)
        # 先 run（创建 supervisor + interrupt）
        sg.run("r1")
        # resume with PASS
        finished = sg.resume_supervisor("r1", "PASS")
        assert finished is True
