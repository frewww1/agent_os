"""GoalGraph 单元测试 — 验证 LangGraph goal 评估循环。"""
import pytest
import tempfile
from unittest.mock import MagicMock

from agent_os.src.core.goal_graph import GoalGraph, GoalState
from agent_os.src.core.models import RunInfo, RunStatus


class TestGoalGraph:
    """验证 GoalGraph 的 graph 结构 + 核心节点逻辑。"""

    def _make_pm(self, eval_result=(True, "goal met")):
        """创建 mock AgentOS。"""
        pm = MagicMock()
        pm._state_dir = tempfile.gettempdir()
        pm._evaluate_goal = MagicMock(return_value=eval_result)
        pm._mark_dirty = MagicMock()
        pm.continue_run = MagicMock()
        # runs.get 返回一个 RunInfo
        ri = RunInfo(run_id="r1", prompt="test", session_id="s1", status=RunStatus.COMPLETED)
        pm.runs = {"r1": ri}
        return pm

    def test_graph_creation(self):
        """GoalGraph 能正常创建（graph 编译成功）。"""
        pm = self._make_pm()
        gg = GoalGraph(pm)
        assert gg._graph is not None

    def test_run_goal_met_first_try(self):
        """goal 首次评估即达成 → run 返回 True，不调 continue_run。"""
        pm = self._make_pm(eval_result=(True, "goal achieved"))
        gg = GoalGraph(pm)
        finished = gg.run("r1", "do something", max_retries=3)
        assert finished is True
        pm._evaluate_goal.assert_called_once()
        pm.continue_run.assert_not_called()  # 达成，不需要反馈重做

    def test_run_goal_not_met_triggers_feedback(self):
        """goal 未达成 → 触发 feedback（continue_run）+ interrupt 暂停。"""
        pm = self._make_pm(eval_result=(False, "not good enough"))
        gg = GoalGraph(pm)
        finished = gg.run("r1", "do something", max_retries=3)
        # interrupt 暂停 → run 返回 False（未完成）
        assert finished is False
        pm._evaluate_goal.assert_called_once()
        pm.continue_run.assert_called_once()  # 触发了反馈

    def test_run_goal_max_retries_exhausted(self):
        """goal 超限 → 不再重试，run 返回 True。"""
        pm = self._make_pm(eval_result=(False, "still not met"))
        gg = GoalGraph(pm)
        # max_retries=0 → 首次评估后即超限
        finished = gg.run("r1", "do something", max_retries=0)
        assert finished is True
        pm._evaluate_goal.assert_called_once()
        pm.continue_run.assert_not_called()  # 超限不反馈

    def test_evaluate_node_uses_pm_evaluate_goal(self):
        """evaluate 节点正确调用 pm._evaluate_goal。"""
        pm = self._make_pm(eval_result=(True, "met"))
        gg = GoalGraph(pm)
        state = GoalState(run_id="r1", goal="test", retries=0, max_retries=3, is_met=False, eval_reason="")
        result = gg._evaluate(state)
        assert result["is_met"] is True
        assert "met" in result["eval_reason"]
        pm._evaluate_goal.assert_called_once()

    def test_feedback_node_calls_continue_run(self):
        """feedback 节点调 continue_run + interrupt。"""
        pm = self._make_pm()
        gg = GoalGraph(pm)
        state = GoalState(run_id="r1", goal="test", retries=0, max_retries=3, is_met=False, eval_reason="not met")
        # interrupt 会暂停 graph — 直接调 _feedback 会触发 interrupt
        # 用 pytest 的 assert_raises 确认 interrupt 被调用
        with pytest.raises(Exception):
            # interrupt 在非 graph 上下文中可能抛异常或返回特殊值
            gg._feedback(state)
        pm.continue_run.assert_called_once()

    def test_route_goal_met_returns_done(self):
        """_route: is_met=True → done。"""
        pm = self._make_pm()
        gg = GoalGraph(pm)
        state = GoalState(run_id="r1", goal="test", retries=0, max_retries=3, is_met=True, eval_reason="")
        assert gg._route(state) == "done"

    def test_route_goal_not_met_returns_feedback(self):
        """_route: is_met=False + retries < max → feedback。"""
        pm = self._make_pm()
        gg = GoalGraph(pm)
        state = GoalState(run_id="r1", goal="test", retries=0, max_retries=3, is_met=False, eval_reason="")
        assert gg._route(state) == "feedback"

    def test_route_retries_exhausted_returns_done(self):
        """_route: retries >= max_retries → done。"""
        pm = self._make_pm()
        gg = GoalGraph(pm)
        state = GoalState(run_id="r1", goal="test", retries=3, max_retries=3, is_met=False, eval_reason="")
        assert gg._route(state) == "done"
