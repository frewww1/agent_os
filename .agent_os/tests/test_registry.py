"""Registry 单元测试 — 可单独运行：pytest tests/test_registry.py"""
import pytest
from agent_os.src.core.registry import Registry
from agent_os.src.core.models import RunInfo, RunStatus, SpawnRequest


class TestRegistry:
    def _make_run(self, run_id="r1", status=RunStatus.RUNNING, parent=None):
        return RunInfo(run_id=run_id, prompt="test", session_id="s1", status=status, parent_run_id=parent)

    def test_register_get(self):
        reg = Registry()
        ri = self._make_run()
        reg.register(ri)
        assert reg.get("r1") is ri
        assert reg.get("nonexistent") is None

    def test_unregister(self):
        reg = Registry()
        ri = self._make_run()
        reg.register(ri)
        removed = reg.unregister("r1")
        assert removed is ri
        assert reg.get("r1") is None
        assert reg.unregister("nonexistent") is None

    def test_list_runs(self):
        reg = Registry()
        reg.register(self._make_run("r1"))
        reg.register(self._make_run("r2"))
        runs = reg.list_runs()
        assert len(runs) == 2
        assert runs[0]["run_id"] in ("r1", "r2")

    def test_get_tree_flat(self):
        reg = Registry()
        reg.register(self._make_run("r1"))
        tree = reg.get_tree()
        assert len(tree) == 1
        assert tree[0]["run_id"] == "r1"
        assert tree[0]["children"] == []

    def test_get_tree_nested(self):
        reg = Registry()
        parent = self._make_run("p1")
        parent.children_run_ids = ["c1"]
        child = self._make_run("c1", parent="p1")
        reg.register(parent)
        reg.register(child)
        tree = reg.get_tree()
        assert len(tree) == 1
        assert tree[0]["run_id"] == "p1"
        assert len(tree[0]["children"]) == 1
        assert tree[0]["children"][0]["run_id"] == "c1"

    def test_link_spawn_get_spawn(self):
        reg = Registry()
        req = reg.link_spawn("sp1", "p1", "sess_p", ["c1", "c2"], "all")
        assert req.spawn_id == "sp1"
        assert req.child_run_ids == ["c1", "c2"]
        assert reg.get_spawn("sp1") is req
        assert reg.get_spawn("nonexistent") is None

    def test_unwrap_task_prompt_from_system(self):
        sp = "## Task\nDo the thing\n\n## Other\nstuff"
        result = Registry.unwrap_task_prompt("irrelevant", sp)
        assert "Do the thing" in result

    def test_unwrap_task_prompt_from_your_task(self):
        prompt = "[Your Task]\nDo something specific\n[/Your Task]"
        result = Registry.unwrap_task_prompt(prompt)
        assert "Do something specific" in result
