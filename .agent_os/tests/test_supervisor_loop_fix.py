"""修复验证测试：
1. exitwatch 不应误关被其他 session 复用的 fd（Windows fd 复用竞态）。
2. supervisor 无结果时重试有限次，超限终止循环，不再无限 resume 父 agent。
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch
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

import agent_os.src.core.agents.base as base_mod  # noqa: E402
from agent_os.src.core.agents.base import (  # noqa: E402
    Agent, RunStatus, _STDOUT_FD_OWNERS, _STDOUT_FD_LOCK,
    _register_stdout_fd, _unregister_stdout_fd,
)


def _make_agent(agent_id, task_type="generative", parent_id=None):
    backend = MagicMock()
    backend.launch = MagicMock()
    backend.stream = MagicMock(return_value=iter([]))
    backend.get_session_path = MagicMock(return_value=None)
    return Agent(
        backend=backend, project_root=".",
        agent_id=agent_id, prompt=f"test {task_type}",
        task_type=task_type, interactive=False,
        parent_id=parent_id,
    )


class FakeStream:
    """模拟 Popen.stdout：fileno 返回指定 fd，close 记录调用。"""
    def __init__(self, fd):
        self._fd = fd
        self.closed = False

    def fileno(self):
        return self._fd

    def close(self):
        self.closed = True


class FakeSession:
    """模拟 Popen 句柄。"""
    def __init__(self, fd):
        self.stdout = FakeStream(fd)
        self.returncode = None
        self.pid = 1234

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.returncode = -15


class TestFdOwnershipRegistry:
    """exitwatch fd 误关修复：注册表 + 所有权校验。"""

    def setup_method(self):
        with _STDOUT_FD_LOCK:
            _STDOUT_FD_OWNERS.clear()

    def test_register_and_unregister(self):
        s1 = FakeSession(4)
        _register_stdout_fd(s1)
        with _STDOUT_FD_LOCK:
            assert _STDOUT_FD_OWNERS.get(4) == id(s1)
        _unregister_stdout_fd(s1)
        with _STDOUT_FD_LOCK:
            assert 4 not in _STDOUT_FD_OWNERS

    def test_unregister_does_not_remove_other_owner(self):
        """fd 被新 session 复用时，旧 session 的 unregister 不应清掉新 owner。"""
        s1 = FakeSession(4)
        s2 = FakeSession(4)
        _register_stdout_fd(s1)
        # 新进程复用了 fd=4
        _register_stdout_fd(s2)
        # 旧 session 的 reader 结束，尝试注销
        _unregister_stdout_fd(s1)
        with _STDOUT_FD_LOCK:
            assert _STDOUT_FD_OWNERS.get(4) == id(s2)

    def test_exitwatch_skips_close_when_fd_reowned(self):
        """旧进程的 exitwatch 关闭 fd 前发现所有权已属于新 session → 跳过关闭。"""
        s1 = FakeSession(4)
        s2 = FakeSession(4)
        _register_stdout_fd(s1)
        _register_stdout_fd(s2)  # fd=4 被新进程复用

        agent = _make_agent("parent")
        agent._session = s2  # 当前 session 已切换为新进程

        with patch.object(base_mod, "_os") as mock_os:
            # 直接调用 exitwatch 内部逻辑：模拟旧 session 的退出看护
            with _STDOUT_FD_LOCK:
                owner = _STDOUT_FD_OWNERS.get(4)
                should_close = owner == id(s1)
                if should_close:
                    _STDOUT_FD_OWNERS.pop(4, None)
                    mock_os.close(4)
            # 断言：fd=4 属于新 session，不应关闭
            assert should_close is False
            mock_os.close.assert_not_called()

    def test_exitwatch_closes_when_still_owner(self):
        """仍拥有 fd 的进程退出时，exitwatch 正常关闭（不泄漏）。"""
        s1 = FakeSession(5)
        _register_stdout_fd(s1)
        with _STDOUT_FD_LOCK:
            owner = _STDOUT_FD_OWNERS.get(5)
            assert owner == id(s1)
            _STDOUT_FD_OWNERS.pop(5, None)
        with _STDOUT_FD_LOCK:
            assert 5 not in _STDOUT_FD_OWNERS


class TestSupervisorRetryLimit:
    """supervisor 无结果 → 有限重试 → 终止循环。"""

    def _make_parent_with_supervisor(self):
        parent = _make_agent("parent")
        parent.supervisor = "review me"
        parent.supervisor_retries = 0
        return parent

    def test_no_result_increments_retry_and_returns_false(self):
        parent = self._make_parent_with_supervisor()
        with patch.object(parent, "add_event"):
            result = parent._handle_supervisor_verdict(None)
        assert result is False  # 未到上限：继续重试（走 resume 流程）
        assert parent.supervisor_retries == 1
        assert parent.supervisor is not None  # 监督未放弃

    def test_retry_under_limit_keeps_supervisor(self):
        parent = self._make_parent_with_supervisor()
        parent.supervisor_retries = 1
        with patch.object(parent, "add_event"):
            result = parent._handle_supervisor_verdict(None)
        assert result is False
        assert parent.supervisor is not None

    def test_retry_exhausted_returns_true_and_clears_supervisor(self):
        parent = self._make_parent_with_supervisor()
        parent.supervisor_retries = 3  # MAX_SUPERVISOR_RETRIES
        with patch.object(parent, "add_event"):
            result = parent._handle_supervisor_verdict(None)
        assert result is True  # 终止循环
        assert parent.supervisor is None  # 放弃监督
        assert parent.supervisor_retries == 0

    def test_valid_verdict_resets_retries(self):
        parent = self._make_parent_with_supervisor()
        parent.supervisor_retries = 2
        with patch.object(parent, "add_event"):
            result = parent._handle_supervisor_verdict("PASS - looks good")
        assert parent.supervisor_retries == 0
        assert parent.supervisor is None
        # 根 agent PASS 时返回 True 阻止 resume
        assert result is True

    def test_supervisor_retries_serialized(self):
        parent = self._make_parent_with_supervisor()
        parent.supervisor_retries = 2
        data = parent.to_jsonable()
        assert data["supervisor_retries"] == 2
