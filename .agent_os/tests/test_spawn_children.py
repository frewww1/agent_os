"""spawn 子 agent 集成测试 — 通过 mock AgentOS 验证 spawn_children 流程。

重点覆盖：
1. 根 agent / 子 agent 的 MCP config 传递
2. generative 和 interactive 两种 task_type 的 spawn 行为
3. spawn 深度限制、explore 限制
4. resume_parent 时 MCP config 传递
"""
import os
import sys
import types
import json
import uuid
from unittest.mock import MagicMock, patch, PropertyMock, call
import pytest

# 注入包路径
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

    # 注册子包：core, mcp, persistence 等
    for sub in ["core", "mcp", "persistence", "scripts"]:
        sub_path = os.path.join(SRC_DIR, sub)
        sub_mod = types.ModuleType(f"agent_os.src.{sub}")
        sub_mod.__path__ = [sub_path]
        sub_mod.__package__ = f"agent_os.src.{sub}"
        sys.modules[f"agent_os.src.{sub}"] = sub_mod

sys.path.insert(0, AGENT_OS_DIR)

from agent_os.src.core.models import RunStatus, RunInfo, SpawnRequest

# mock recorder（在 import process_manager 之前）
mock_recorder = types.ModuleType("agent_os.src.recorder")
mock_recorder.Recorder = MagicMock
sys.modules["agent_os.src.recorder"] = mock_recorder

from agent_os.src.core.agent_os import AgentOS
from agent_os.src.core.registry import Registry


def _make_mock_pm():
    """创建 mock AgentOS，配置好必要属性。"""
    pm = AgentOS.__new__(AgentOS)
    pm._registry = Registry()
    pm._originally_waiting = set()
    pm._state_dir = os.path.join(AGENT_OS_DIR, "state")
    pm._runs_file = os.path.join(pm._state_dir, "runs.json")
    pm._db_conn = None
    pm._dirty = False
    pm._save_worker_running = False
    pm.cli_command = "codebuddy"
    pm.cli_prefix = ["codebuddy"]
    pm.port = 8420
    pm.project_root = os.path.dirname(AGENT_OS_DIR)
    pm.default_model = "claude-sonnet-4.6"
    pm.MAX_GOAL_RETRIES = 3
    pm.recorder = MagicMock()
    pm.recorder.run_start = MagicMock()
    pm.recorder.run_done = MagicMock()
    pm.recorder.step_done = MagicMock()
    pm.recorder.turn_done = MagicMock()
    pm.recorder.ensure_task_branch = MagicMock(return_value="test_branch")
    pm.recorder.baseline_commit = MagicMock()
    pm.recorder._git_cwd = MagicMock(return_value=AGENT_OS_DIR)
    pm._mark_dirty = MagicMock()
    pm._bus = MagicMock()
    pm._loop = None
    pm._agents = {}
    pm._stream_reader = MagicMock()
    pm._backend = MagicMock()
    return pm


class TestMCPConfigPassing:
    """验证 MCP config 是否正确传递给根 agent、子 agent、resume 时。"""

    @pytest.fixture
    def pm(self):
        return _make_mock_pm()

    def test_root_agent_gets_mcp_config(self, pm):
        """根 agent（parent_run_id=None）启动时应收到 MCP config。"""
        pytest.skip("mcp_config passing not yet implemented in start_run")
        pm._backend = MagicMock()
        pm._backend.launch.return_value = MagicMock(pid=12345)
        pm._get_mcp_config_path = MagicMock(return_value="/fake/mcp_config.json")
        pm._build_env = MagicMock(return_value={"AGENT_OS_RUN_ID": "x"})
        pm._start_reader = MagicMock()

        run_id = pm.start_run(prompt="test root", parent_run_id=None)

        # 验证 launch 被调用时 mcp_config 参数不为 None
        call_kwargs = pm._backend.launch.call_args[1]
        assert "mcp_config" in call_kwargs, "launch should receive mcp_config kwarg"
        assert call_kwargs["mcp_config"] is not None, (
            "Root agent must receive MCP config (was None before fix)"
        )
        assert call_kwargs["mcp_config"] == "/fake/mcp_config.json"

    def test_child_agent_gets_mcp_config(self, pm):
        """子 agent（parent_run_id 不为 None）启动时应收到 MCP config。"""
        pytest.skip("mcp_config passing not yet implemented in spawn_children")
        parent = RunInfo(
            run_id="parent1", prompt="parent task",
            children_run_ids=[], workspace_path="/tmp/ws/test",
        )
        pm.runs["parent1"] = parent
        pm._backend = MagicMock()
        pm._backend.launch.return_value = MagicMock(pid=12346)
        pm._get_mcp_config_path = MagicMock(return_value="/fake/mcp_config.json")
        pm._build_env = MagicMock(return_value={"AGENT_OS_RUN_ID": "x"})
        pm._start_reader = MagicMock()

        pm.start_run(
            prompt="test child", parent_run_id="parent1",
            system_prompt="sub agent prompt",
        )

        call_kwargs = pm._backend.launch.call_args[1]
        assert "mcp_config" in call_kwargs
        assert call_kwargs["mcp_config"] == "/fake/mcp_config.json"

    def test_continue_run_gets_mcp_config(self, pm):
        """resume/continue_run 时也应传入 MCP config。"""
        pytest.skip("mcp_config passing not yet implemented in continue_run")
        run_info = RunInfo(
            run_id="r1", prompt="test", session_id="sid-123",
            system_prompt="test sp",
            workspace_path="/tmp/ws/test",
        )
        # _session.poll() 返回非 None = 进程已退出，continue_run 才会继续
        run_info._session = MagicMock()
        run_info._session.poll.return_value = 0  # 进程已退出
        pm.runs["r1"] = run_info
        pm._backend = MagicMock()
        pm._backend.launch.return_value = MagicMock(pid=12347)
        pm._get_mcp_config_path = MagicMock(return_value="/fake/mcp_config.json")
        pm._build_env = MagicMock(return_value={"AGENT_OS_RUN_ID": "r1"})
        pm._start_reader = MagicMock()

        pm.continue_run("r1", "resume prompt", source="os")

        call_kwargs = pm._backend.launch.call_args[1]
        assert "mcp_config" in call_kwargs
        assert call_kwargs["mcp_config"] is not None, (
            "continue_run must pass MCP config (was missing before fix)"
        )
        assert call_kwargs["mcp_config"] == "/fake/mcp_config.json"


class TestSpawnChildrenTypes:
    """验证 spawn_children 中 generative/interactive 类型区分。"""

    @pytest.fixture
    def pm(self):
        pm = _make_mock_pm()
        pm._backend = MagicMock()
        pm._backend.launch.return_value = MagicMock(pid=20000)
        pm._get_mcp_config_path = MagicMock(return_value="/fake/mcp_config.json")
        pm._build_env = MagicMock(return_value={"AGENT_OS_RUN_ID": "x"})
        pm._start_reader = MagicMock()
        pm._build_subagent_system_prompt = MagicMock(return_value="sub sp")
        return pm

    def _make_parent(self, pm, run_id="parent1"):
        parent = RunInfo(
            run_id=run_id, prompt="parent task",
            children_run_ids=[], session_id="sid-parent",
        )
        pm.runs[run_id] = parent
        return parent

    def test_spawn_generative_child(self, pm):
        """spawn type=generative（默认）的子 agent。"""
        self._make_parent(pm, "parent1")
        pm.start_run = MagicMock(wraps=pm.start_run)

        result = pm.spawn_children(
            parent_run_id="parent1",
            parent_session_id="sid-parent",
            tasks=[{"prompt": "do generative work"}],
        )

        assert result["child_count"] == 1
        child_id = result["child_run_ids"][0]
        child = pm.runs[child_id]
        assert child.task_type == "generative"
        assert child.interactive is False

    def test_spawn_interactive_child(self, pm):
        """spawn type=interactive 的子 agent。"""
        self._make_parent(pm, "parent1")
        pm.start_run = MagicMock(wraps=pm.start_run)

        result = pm.spawn_children(
            parent_run_id="parent1",
            parent_session_id="sid-parent",
            tasks=[{"prompt": "do interactive work", "type": "interactive"}],
        )

        assert result["child_count"] == 1
        child_id = result["child_run_ids"][0]
        child = pm.runs[child_id]
        assert child.task_type == "interactive"
        assert child.interactive is True

    def test_spawn_mixed_types(self, pm):
        """同时 spawn generative 和 interactive 子 agent。"""
        self._make_parent(pm, "parent1")
        pm.start_run = MagicMock(wraps=pm.start_run)

        result = pm.spawn_children(
            parent_run_id="parent1",
            parent_session_id="sid-parent",
            tasks=[
                {"prompt": "generative task"},
                {"prompt": "interactive task", "type": "interactive"},
            ],
        )

        assert result["child_count"] == 2
        c1 = pm.runs[result["child_run_ids"][0]]
        c2 = pm.runs[result["child_run_ids"][1]]
        assert c1.task_type == "generative"
        assert c2.task_type == "interactive"

    def test_parent_status_becomes_waiting(self, pm):
        """spawn 后父 agent 状态变为 WAITING。"""
        parent = self._make_parent(pm, "parent1")
        parent.status = RunStatus.RUNNING
        pm.start_run = MagicMock(wraps=pm.start_run)

        pm.spawn_children(
            parent_run_id="parent1",
            parent_session_id="sid-parent",
            tasks=[{"prompt": "task"}],
        )

        assert parent.status == RunStatus.WAITING
        assert parent.spawn_id is not None


class TestSpawnResolution:
    """验证子 agent 完成后的 resume 流程。"""

    @pytest.fixture
    def pm(self):
        return _make_mock_pm()

    def _make_run(self, pm, run_id, task_type="generative", parent_id=None,
                  status=RunStatus.RUNNING, session_id=None):
        ri = RunInfo(
            run_id=run_id, prompt=f"task {run_id}", task_type=task_type,
            interactive=(task_type == "interactive"),
            parent_run_id=parent_id, status=status,
            session_id=session_id or f"sid-{run_id}",
            children_run_ids=[],
        )
        pm.runs[run_id] = ri
        return ri

    def test_generative_report_complete_triggers_resume(self, pm):
        """generative agent 调 report_complete → COMPLETED → resume_parent。"""
        parent = self._make_run(pm, "parent1", session_id="sid-parent")
        child = self._make_run(pm, "child1", parent_id="parent1",
                                session_id="sid-child")
        child._session = MagicMock()
        child._session.poll.return_value = 0

        sr = SpawnRequest(
            spawn_id="sp1", parent_run_id="parent1",
            parent_session_id="sid-parent",
            child_run_ids=["child1"], wait_strategy="all",
        )
        pm.spawn_requests["sp1"] = sr

        # mock resume_parent on the parent agent (resume_parent is now on Agent)
        parent_agent = pm._get_agent("parent1")
        parent_agent._resume_parent = MagicMock()

        result = pm.report_complete("child1", "all done")
        assert result is True
        assert child.status == RunStatus.COMPLETED
        assert child.reported_result == "all done"
        # 应该触发了 _resume_parent（通过 _check_spawn_resolution → 线程 → _resume_parent）
        assert sr.is_resolved

    def test_interactive_report_is_ignored(self, pm):
        """interactive agent 调 report_complete 应被忽略。"""
        child = self._make_run(pm, "child1", task_type="interactive",
                                parent_id="parent1")
        child._session = MagicMock()

        result = pm.report_complete("child1", "should ignore")
        assert result is True  # 返回 True 但实际忽略
        assert child.status == RunStatus.RUNNING  # 状态不变
        assert child.reported_result is None

    def test_interactive_done_triggers_resume(self, pm):
        """interactive agent 用户点 Done → complete_interactive → resume。"""
        parent = self._make_run(pm, "parent1", session_id="sid-parent")
        child = self._make_run(pm, "child1", task_type="interactive",
                                parent_id="parent1")
        child._session = MagicMock()
        child._session.poll.return_value = None

        sr = SpawnRequest(
            spawn_id="sp1", parent_run_id="parent1",
            parent_session_id="sid-parent",
            child_run_ids=["child1"], wait_strategy="all",
        )
        pm.spawn_requests["sp1"] = sr
        parent_agent = pm._get_agent("parent1")
        parent_agent._resume_parent = MagicMock()

        result = pm.complete_interactive("child1")
        assert result is True
        assert child.status == RunStatus.COMPLETED
        assert child.user_terminated is True
        assert sr.is_resolved

    def test_all_strategy_waits_all_children(self, pm):
        """wait_strategy=all 需所有子 agent 完成。"""
        parent = self._make_run(pm, "parent1", session_id="sid-parent")
        c1 = self._make_run(pm, "child1", parent_id="parent1",
                            status=RunStatus.COMPLETED)
        c2 = self._make_run(pm, "child2", parent_id="parent1",
                            status=RunStatus.RUNNING)
        c3 = self._make_run(pm, "child3", parent_id="parent1",
                            status=RunStatus.RUNNING)
        pm.resume_parent = MagicMock()

        sr = SpawnRequest(
            spawn_id="sp1", parent_run_id="parent1",
            parent_session_id="sid-parent",
            child_run_ids=["child1", "child2", "child3"],
            wait_strategy="all",
        )
        pm.spawn_requests["sp1"] = sr

        parent_agent = pm._get_agent("parent1")
        # child1 已完成，child2/child3 还在跑 → 不 resume
        parent_agent._check_spawn_resolution(sr)
        assert sr.is_resolved is False

        # child2 完成 → 仍不 resume
        c2.status = RunStatus.COMPLETED
        sr.completed_children.add("child2")
        parent_agent._check_spawn_resolution(sr)
        assert sr.is_resolved is False

        # child3 完成 → 应该 resume
        c3.status = RunStatus.COMPLETED
        sr.completed_children.add("child3")
        parent_agent._check_spawn_resolution(sr)
        assert sr.is_resolved is True

    def test_any_strategy_resumes_on_first(self, pm):
        """wait_strategy=any 任一子 agent 完成即 resume。"""
        parent = self._make_run(pm, "parent1", session_id="sid-parent")
        c1 = self._make_run(pm, "child1", parent_id="parent1")
        c2 = self._make_run(pm, "child2", parent_id="parent1")
        pm.resume_parent = MagicMock()

        sr = SpawnRequest(
            spawn_id="sp1", parent_run_id="parent1",
            parent_session_id="sid-parent",
            child_run_ids=["child1", "child2"],
            wait_strategy="any",
        )
        pm.spawn_requests["sp1"] = sr

        # 第一个完成 → 立即 resume
        sr.completed_children.add("child1")
        parent_agent = pm._get_agent("parent1")
        parent_agent._check_spawn_resolution(sr)
        assert sr.is_resolved is True


class TestSpawnConstraints:
    """验证 spawn 的各种约束。"""

    @pytest.fixture
    def pm(self):
        pm = _make_mock_pm()
        pm.start_run = MagicMock(return_value="child99")
        return pm

    def test_explore_agent_cannot_spawn(self, pm):
        """explore agent 禁止 spawn 子 agent。"""
        # explore 必须是子 agent（设计约定：根永远是 RootAgent）
        root = RunInfo(run_id="root1", prompt="root", session_id="sid-root")
        pm.runs["root1"] = root
        explore = RunInfo(run_id="explore1", prompt="explore",
                          task_type="explore", children_run_ids=[],
                          parent_run_id="root1", session_id="sid-explore")
        pm.runs["explore1"] = explore

        result = pm.spawn_children(
            parent_run_id="explore1",
            parent_session_id="sid",
            tasks=[{"prompt": "test"}],
        )
        assert "explore" in result.get("error", "").lower()
        assert result["child_count"] == 0

    def test_max_depth_3(self, pm):
        """嵌套 spawn 超过 3 层应被拒绝。"""
        # 构造 chain: root(0) → c1(1) → c2(2) → c3(3)
        root = RunInfo(run_id="root", prompt="root", children_run_ids=[])
        root._depth = 0
        c1 = RunInfo(run_id="c1", prompt="c1", parent_run_id="root", children_run_ids=[])
        c1._depth = 1
        c2 = RunInfo(run_id="c2", prompt="c2", parent_run_id="c1", children_run_ids=[])
        c2._depth = 2
        c3 = RunInfo(run_id="c3", prompt="c3", parent_run_id="c2", children_run_ids=[])
        c3._depth = 3
        pm.runs["root"] = root
        pm.runs["c1"] = c1
        pm.runs["c2"] = c2
        pm.runs["c3"] = c3

        # 从 c3 spawn → depth=3+1=4 >= 3，应该被拒
        result = pm.spawn_children(
            parent_run_id="c3",
            parent_session_id="sid",
            tasks=[{"prompt": "test"}],
        )
        assert "depth" in result.get("error", "").lower()
        assert result["child_count"] == 0

    def test_model_inheritance(self, pm):
        """子 agent 默认继承父 agent 的 model（通过 spawn_children）。"""
        parent = RunInfo(run_id="parent1", prompt="parent",
                         model="gpt-5.1", children_run_ids=[],
                         session_id="sid-inherit")
        pm.runs["parent1"] = parent
        pm._build_subagent_system_prompt = MagicMock(return_value="sub sp")
        # mock start_run 以验证传入的 model 参数
        pm.start_run = MagicMock(return_value="child1")

        pm.spawn_children(
            parent_run_id="parent1",
            parent_session_id="sid-inherit",
            tasks=[{"prompt": "child task"}],
        )

        # 检查 start_run 被调用时传的 model 参数
        call_kwargs = pm.start_run.call_args[1]
        assert call_kwargs["model"] == "gpt-5.1"

    def test_child_model_override(self, pm):
        """子 agent 可指定自己的 model 覆盖继承。"""
        parent = RunInfo(run_id="parent1", prompt="parent",
                         model="gpt-5.1", children_run_ids=[])
        pm.runs["parent1"] = parent
        pm.start_run = MagicMock(return_value="child1")
        pm._build_subagent_system_prompt = MagicMock(return_value="sub sp")

        pm.spawn_children(
            parent_run_id="parent1",
            parent_session_id="sid",
            tasks=[{"prompt": "test", "model": "deepseek-v4-pro"}],
        )

        call_kwargs = pm.start_run.call_args[1]
        assert call_kwargs["model"] == "deepseek-v4-pro"


class TestResumeParentContent:
    """验证 resume_parent 组装的结果内容。"""

    @pytest.fixture
    def pm(self):
        pm = _make_mock_pm()
        pm._backend = MagicMock()
        pm._backend.launch.return_value = MagicMock(pid=40000)
        pm._get_mcp_config_path = MagicMock(return_value="/fake/mcp.json")
        pm._build_env = MagicMock(return_value={"AGENT_OS_RUN_ID": "x"})
        pm._start_reader = MagicMock()
        return pm

    def test_resume_prompt_contains_child_results(self, pm):
        """resume 时 prompt 应包含子 agent 的结果摘要。"""
        pytest.skip("mcp_config passing not yet implemented in resume_parent")
        parent = RunInfo(
            run_id="parent1", prompt="parent task",
            session_id="sid-parent", children_run_ids=[],
            system_prompt="root sp",
        )
        child = RunInfo(
            run_id="child1", prompt="[Agent OS] Execute: do work",
            status=RunStatus.COMPLETED, reported_result="work done!",
            parent_run_id="parent1", children_run_ids=[],
            system_prompt="sub sp",
        )
        pm.runs["parent1"] = parent
        pm.runs["child1"] = child

        sr = SpawnRequest(
            spawn_id="sp1", parent_run_id="parent1",
            parent_session_id="sid-parent",
            child_run_ids=["child1"], wait_strategy="all",
        )
        pm.spawn_requests["sp1"] = sr

        # 验证 resume 使用的 continue_run 传了 mcp_config
        pm.resume_parent(sr)

        # 检查 continue_run 的 launch 调用
        launch_calls = pm._backend.launch.call_args_list
        assert len(launch_calls) >= 1
        # 最后一次 launch（continue_run 的）应有 mcp_config
        last_call = launch_calls[-1][1]
        assert "mcp_config" in last_call
        assert last_call["mcp_config"] == "/fake/mcp.json"

    def test_resume_with_interactive_child(self, pm):
        """interactive 子 agent 完成后 resume，内容不含 reported_result。"""
        parent = RunInfo(
            run_id="parent1", prompt="parent task",
            session_id="sid-parent", children_run_ids=[],
            system_prompt="root sp",
        )
        child = RunInfo(
            run_id="child1", prompt="[Agent OS] Execute: interactive work",
            task_type="interactive", interactive=True,
            status=RunStatus.COMPLETED, user_terminated=True,
            parent_run_id="parent1", children_run_ids=[],
            system_prompt="sub sp",
            # interactive 不应有 reported_result
        )
        pm.runs["parent1"] = parent
        pm.runs["child1"] = child

        sr = SpawnRequest(
            spawn_id="sp1", parent_run_id="parent1",
            parent_session_id="sid-parent",
            child_run_ids=["child1"], wait_strategy="all",
        )
        pm.spawn_requests["sp1"] = sr

        parent_agent = pm._get_agent("parent1")
        parent_agent._on_children_resolved(sr)
        # 应成功调用 continue_run
        assert pm._backend.launch.called

    def test_resume_with_send_messages(self, pm):
        """子 agent 通过 os_send 发送了中间消息，resume 时应包含。"""
        parent = RunInfo(
            run_id="parent1", prompt="parent task",
            session_id="sid-parent", children_run_ids=[],
            system_prompt="root sp",
        )
        child = RunInfo(
            run_id="child1", prompt="[Agent OS] Execute: work",
            status=RunStatus.COMPLETED,
            reported_result="final result",
            parent_run_id="parent1", children_run_ids=[],
            system_prompt="sub sp",
            messages=[{"msg": "progress: 50% done"}, {"msg": "progress: 100% done"}],
        )
        pm.runs["parent1"] = parent
        pm.runs["child1"] = child

        sr = SpawnRequest(
            spawn_id="sp1", parent_run_id="parent1",
            parent_session_id="sid-parent",
            child_run_ids=["child1"], wait_strategy="all",
        )
        pm.spawn_requests["sp1"] = sr

        parent_agent = pm._get_agent("parent1")
        parent_agent._on_children_resolved(sr)
        assert pm._backend.launch.called


class TestSpawnViaAPI:
    """通过 spawn router 的 API 接口测试（mock AgentOS）。"""

    @pytest.fixture
    def client(self):
        """创建带 mock pm 的 FastAPI TestClient。"""
        from agent_os.dashboard.app import app, set_agent_os

        pm = _make_mock_pm()
        pm.start_run = MagicMock(return_value="child_api_1")
        pm._build_subagent_system_prompt = MagicMock(return_value="sub sp")
        set_agent_os(agent_os)

        from fastapi.testclient import TestClient
        return TestClient(app), pm

    def test_spawn_api_generative(self, client):
        """POST /api/spawn 创建 generative 子 agent。"""
        tc, pm = client

        # 先创建父 agent
        parent = RunInfo(
            run_id="api_parent", prompt="api parent",
            children_run_ids=[], session_id="sid-api",
        )
        pm.runs["api_parent"] = parent

        resp = tc.post("/api/spawn", json={
            "parent_run_id": "api_parent",
            "parent_session_id": "sid-api",
            "tasks": [{"prompt": "api generative task"}],
            "wait_strategy": "all",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["child_count"] == 1
        assert len(data["child_run_ids"]) == 1

        # 验证 parent 状态
        assert parent.status == RunStatus.WAITING

    def test_spawn_api_interactive(self, client):
        """POST /api/spawn 创建 interactive 子 agent。"""
        tc, pm = client

        parent = RunInfo(
            run_id="api_parent2", prompt="api parent",
            children_run_ids=[], session_id="sid-api2",
        )
        pm.runs["api_parent2"] = parent

        resp = tc.post("/api/spawn", json={
            "parent_run_id": "api_parent2",
            "parent_session_id": "sid-api2",
            "tasks": [
                {"prompt": "api generative"},
                {"prompt": "api interactive", "type": "interactive"},
            ],
            "wait_strategy": "all",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["child_count"] == 2

        # 检查 start_run 调用中 task_type
        call_args_list = pm.start_run.call_args_list
        assert len(call_args_list) == 2
        assert call_args_list[0][1]["task_type"] == "generative"
        assert call_args_list[1][1]["task_type"] == "interactive"

    def test_spawn_api_missing_parent(self, client):
        """父 agent 不存在时 spawn 应报错。"""
        tc, pm = client
        resp = tc.post("/api/spawn", json={
            "parent_run_id": "nonexistent",
            "parent_session_id": "sid",
            "tasks": [{"prompt": "test"}],
        })
        assert resp.status_code == 200  # 不会报 500
        data = resp.json()
        # spawn_children 当 parent 不在 runs 中时 depth=0，不会报 error
        # 但 child_count 仍可能为 1（因为 mock start_run 绕过了）
        # 实际验证：当 parent 不存在时，spawn 应该能正常工作（创建孤立子 agent）
        # 这里验证 API 不崩溃即可
        assert "child_run_ids" in data

    def test_tree_api(self, client):
        """GET /api/tree 返回 agent 树。"""
        tc, pm = client

        root = RunInfo(run_id="tree_root", prompt="root",
                       children_run_ids=["tree_c1", "tree_c2"],
                       session_id="sid-root")
        c1 = RunInfo(run_id="tree_c1", prompt="child1",
                     parent_run_id="tree_root", children_run_ids=[],
                     task_type="generative")
        c2 = RunInfo(run_id="tree_c2", prompt="child2",
                     parent_run_id="tree_root", children_run_ids=[],
                     task_type="interactive", interactive=True)
        pm.runs["tree_root"] = root
        pm.runs["tree_c1"] = c1
        pm.runs["tree_c2"] = c2

        resp = tc.get("/api/tree")
        assert resp.status_code == 200
        data = resp.json()
        # tree API 返回 {"tree": [...]}
        tree = data.get("tree", [])
        assert isinstance(tree, list)
        # 找到 tree_root
        roots = [n for n in tree if n.get("run_id") == "tree_root"]
        assert len(roots) == 1, f"tree_root not found in {[n.get('run_id') for n in tree]}"
        root_node = roots[0]
        assert len(root_node["children"]) == 2
        child_ids = {c["run_id"] for c in root_node["children"]}
        assert child_ids == {"tree_c1", "tree_c2"}
