"""P0 测试：dag_planner.py 核心逻辑覆盖。

涵盖：
  - topo_order：线性/菱形/环检测/空图/孤立节点
  - ready_steps：初始状态 / 部分完成 / 全部完成
  - get_descendants：单节点/链式/菱形下游
  - add_step：正常追加/重复 id/缺失依赖/追加后成环/缺 id
  - reset_steps：批量重置 status 与时间戳
  - mark_running / mark_done / mark_failed：状态与时间戳
  - load_dag / save_dag：文件 I/O 往返
"""
import copy
import json
import os
import sys

import pytest

# 让测试可以直接 import dag_planner（不依赖 package install）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import dag_planner as dp


# ---------------------------------------------------------------------------
# topo_order
# ---------------------------------------------------------------------------

class TestTopoOrder:
    def test_linear(self, simple_steps):
        order = dp.topo_order(simple_steps)
        assert order == ["A", "B", "C"]

    def test_diamond(self, diamond_steps):
        order = dp.topo_order(diamond_steps)
        # A 必须最先，D 必须最后
        assert order[0] == "A"
        assert order[-1] == "D"
        assert set(order) == {"A", "B", "C", "D"}

    def test_cyclic_raises(self, cyclic_steps):
        with pytest.raises(ValueError, match="cycles"):
            dp.topo_order(cyclic_steps)

    def test_empty(self):
        assert dp.topo_order([]) == []

    def test_single_node(self):
        steps = [{"id": "solo", "depends_on": []}]
        assert dp.topo_order(steps) == ["solo"]

    def test_unknown_dep_ignored(self):
        """depends_on 引用不存在的节点时应被过滤，不抛异常。"""
        steps = [
            {"id": "X", "depends_on": ["ghost"]},
        ]
        order = dp.topo_order(steps)
        assert order == ["X"]


# ---------------------------------------------------------------------------
# ready_steps
# ---------------------------------------------------------------------------

class TestReadySteps:
    def test_all_pending_only_roots_ready(self, simple_steps):
        ready = dp.ready_steps(simple_steps)
        assert ready == ["A"]

    def test_after_a_done_b_becomes_ready(self, simple_steps):
        simple_steps[0]["status"] = "done"   # A done
        ready = dp.ready_steps(simple_steps)
        assert ready == ["B"]

    def test_all_done_nothing_ready(self, simple_steps):
        for s in simple_steps:
            s["status"] = "done"
        assert dp.ready_steps(simple_steps) == []

    def test_running_step_not_ready(self, simple_steps):
        simple_steps[0]["status"] = "running"
        ready = dp.ready_steps(simple_steps)
        # A 是 running，B 的前置未 done，所以 B 也不 ready
        assert ready == []

    def test_diamond_both_branches_ready_after_a(self, diamond_steps):
        diamond_steps[0]["status"] = "done"   # A done
        ready = dp.ready_steps(diamond_steps)
        assert set(ready) == {"B", "C"}


# ---------------------------------------------------------------------------
# get_descendants
# ---------------------------------------------------------------------------

class TestGetDescendants:
    def test_leaf_node_no_children(self, simple_steps):
        desc = dp.get_descendants(simple_steps, "C")
        assert desc == ["C"]

    def test_root_returns_all(self, simple_steps):
        desc = dp.get_descendants(simple_steps, "A")
        assert desc == ["A", "B", "C"]

    def test_middle_node(self, simple_steps):
        desc = dp.get_descendants(simple_steps, "B")
        assert desc == ["B", "C"]

    def test_diamond_root(self, diamond_steps):
        desc = dp.get_descendants(diamond_steps, "A")
        assert set(desc) == {"A", "B", "C", "D"}
        assert desc[-1] == "D"   # D 在最后（拓扑序）


# ---------------------------------------------------------------------------
# add_step
# ---------------------------------------------------------------------------

class TestAddStep:
    def test_append_leaf(self, simple_steps):
        steps = copy.deepcopy(simple_steps)
        new = dp.add_step(steps, {"id": "D", "depends_on": ["C"]})
        assert new["id"] == "D"
        assert new["status"] == "pending"
        assert len(steps) == 4

    def test_duplicate_id_raises(self, simple_steps):
        steps = copy.deepcopy(simple_steps)
        with pytest.raises(ValueError, match="already exists"):
            dp.add_step(steps, {"id": "A", "depends_on": []})

    def test_missing_dep_raises(self, simple_steps):
        steps = copy.deepcopy(simple_steps)
        with pytest.raises(ValueError, match="unknown steps"):
            dp.add_step(steps, {"id": "D", "depends_on": ["Z"]})

    def test_would_create_cycle_raises(self):
        """构造一个追加后会形成环的场景：
        已有 X -> Y，追加 Z depends_on=[Y]，再追加 W depends_on=[Z]，
        然后尝试追加一个 depends_on 包含尚未存在节点的步骤（由 missing dep 校验拦截），
        属于 add_step 校验的一部分。
        更直接的环场景：手工绕过 add_step 在 steps 里构造环，再验证 topo_order 抛出。
        """
        # 手工构造追加后会导致环的 steps（绕过 add_step 的前置校验直接构造）
        # 验证点：topo_order 对有环图抛 ValueError
        steps = [
            {"id": "X", "depends_on": ["Z"], "status": "pending"},
            {"id": "Y", "depends_on": ["X"], "status": "pending"},
            {"id": "Z", "depends_on": ["Y"], "status": "pending"},
        ]
        with pytest.raises(ValueError, match="cycles"):
            dp.topo_order(steps)

    def test_missing_id_raises(self, simple_steps):
        steps = copy.deepcopy(simple_steps)
        with pytest.raises(ValueError, match="id"):
            dp.add_step(steps, {"depends_on": []})

    def test_defaults_filled(self, simple_steps):
        steps = copy.deepcopy(simple_steps)
        new = dp.add_step(steps, {"id": "D"})
        assert new["name"] == "D"
        assert new["prompt"] == ""
        assert new["agent_name"] is None
        assert new["model"] is None


# ---------------------------------------------------------------------------
# reset_steps
# ---------------------------------------------------------------------------

class TestResetSteps:
    def test_reset_clears_status_and_timestamps(self):
        steps = [
            {"id": "A", "status": "done", "started_at": "t1", "completed_at": "t2"},
            {"id": "B", "status": "failed", "started_at": "t3", "completed_at": "t4"},
        ]
        hit = dp.reset_steps(steps, ["A", "B"])
        assert hit == ["A", "B"]
        for s in steps:
            assert s["status"] == "pending"
            assert "started_at" not in s
            assert "completed_at" not in s

    def test_reset_unknown_id_skipped(self):
        steps = [{"id": "A", "status": "done"}]
        hit = dp.reset_steps(steps, ["A", "ghost"])
        assert hit == ["A"]


# ---------------------------------------------------------------------------
# mark_running / mark_done / mark_failed
# ---------------------------------------------------------------------------

class TestStatusTransitions:
    def _make_steps(self):
        return [{"id": "X", "status": "pending"}]

    def test_mark_running(self):
        steps = self._make_steps()
        result = dp.mark_running(steps, "X")
        assert result is True
        assert steps[0]["status"] == "running"
        assert "started_at" in steps[0]

    def test_mark_done(self):
        steps = self._make_steps()
        result = dp.mark_done(steps, "X")
        assert result is True
        assert steps[0]["status"] == "done"
        assert "completed_at" in steps[0]

    def test_mark_failed(self):
        steps = self._make_steps()
        result = dp.mark_failed(steps, "X")
        assert result is True
        assert steps[0]["status"] == "failed"
        assert "completed_at" in steps[0]

    def test_unknown_id_returns_false(self):
        steps = self._make_steps()
        assert dp.mark_done(steps, "nobody") is False
        assert dp.mark_running(steps, "nobody") is False
        assert dp.mark_failed(steps, "nobody") is False

    def test_mark_running_clears_completed_at(self):
        steps = [{"id": "X", "status": "done", "completed_at": "old"}]
        dp.mark_running(steps, "X")
        assert "completed_at" not in steps[0]


# ---------------------------------------------------------------------------
# load_dag / save_dag
# ---------------------------------------------------------------------------

class TestDagIO:
    def test_load_missing_file_returns_empty(self, tmp_path):
        dag = dp.load_dag(str(tmp_path))
        assert dag == {"steps": []}

    def test_save_then_load_roundtrip(self, tmp_path):
        original = {"steps": [{"id": "A", "depends_on": [], "status": "pending"}]}
        dp.save_dag(str(tmp_path), original)
        loaded = dp.load_dag(str(tmp_path))
        assert loaded == original

    def test_save_creates_valid_json(self, tmp_path):
        dag = {"steps": [{"id": "X", "name": "中文名称", "depends_on": []}]}
        dp.save_dag(str(tmp_path), dag)
        path = tmp_path / "dag.json"
        text = path.read_text(encoding="utf-8")
        parsed = json.loads(text)
        assert parsed["steps"][0]["name"] == "中文名称"
