"""FastAPI 路由层测试。

由于 dashboard/app.py 使用相对导入，测试时需要先把整个包结构注入到 sys.modules。
"""
import sys
import os
import types
from unittest.mock import MagicMock
import pytest

AGENT_OS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _build_package_in_sys_modules():
    if "agent_os" in sys.modules and isinstance(sys.modules["agent_os"], types.ModuleType):
        return

    pkg = types.ModuleType("agent_os")
    pkg.__path__ = [AGENT_OS_DIR]
    pkg.__package__ = "agent_os"
    pkg.__spec__ = None
    sys.modules["agent_os"] = pkg

    mock_agent_os = types.ModuleType("agent_os.agent_os")
    mock_agent_os.AgentOS = MagicMock
    sys.modules["agent_os.agent_os"] = mock_agent_os

    dash_pkg = types.ModuleType("agent_os.dashboard")
    dash_pkg.__path__ = [os.path.join(AGENT_OS_DIR, "dashboard")]
    dash_pkg.__package__ = "agent_os.dashboard"
    sys.modules["agent_os.dashboard"] = dash_pkg

    src_pkg = types.ModuleType("agent_os.src")
    src_pkg.__path__ = [os.path.join(AGENT_OS_DIR, "src")]
    src_pkg.__package__ = "agent_os.src"
    sys.modules["agent_os.src"] = src_pkg

    mock_utils = types.ModuleType("agent_os.src.utils")
    mock_utils.safe_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    sys.modules["agent_os.src.utils"] = mock_utils

    mock_src_core = types.ModuleType("agent_os.src.core")
    mock_src_core.__path__ = [os.path.join(AGENT_OS_DIR, "src", "core")]
    mock_src_core.__package__ = "agent_os.src.core"
    sys.modules["agent_os.src.core"] = mock_src_core

    mock_src_core_agent_os = types.ModuleType("agent_os.src.core.agent_os")
    mock_src_core_agent_os.AgentOS = MagicMock
    sys.modules["agent_os.src.core.agent_os"] = mock_src_core_agent_os


_build_package_in_sys_modules()

import importlib.util  # noqa: E402


def _load_app_module():
    app_path = os.path.join(AGENT_OS_DIR, "dashboard", "app.py")
    spec = importlib.util.spec_from_file_location(
        "agent_os.dashboard.app", app_path,
        submodule_search_locations=[]
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "agent_os.dashboard"
    sys.modules["agent_os.dashboard.app"] = mod
    spec.loader.exec_module(mod)
    return mod


_app_mod = _load_app_module()
app = _app_mod.app

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def mock_pm():
    m = MagicMock()
    m.agents = {}
    m.list_agents.return_value = []
    m.list_models.return_value = []
    m.get_workspace_path = MagicMock(return_value=None)
    m.MAX_GOAL_RETRIES = 5
    _app_mod.set_agent_os(m)
    return m


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# -------------------------------------------------------
# GET /api/agents  →  {"agents": [...]}
# -------------------------------------------------------

class TestListAgents:
    def test_empty_agents_returns_list(self, client, mock_pm):
        mock_pm.agents = {}
        mock_pm.list_agents.return_value = []
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        body = resp.json()
        assert "agents" in body
        assert isinstance(body["agents"], list)


# -------------------------------------------------------
# GET /api/agent/{agent_id}
# -------------------------------------------------------

class TestGetAgent:
    def test_missing_agent_returns_404(self, client, mock_pm):
        mock_pm.get_agent.return_value = None
        resp = client.get("/api/agent/nonexistent")
        assert resp.status_code == 404

    def test_existing_agent_returns_200(self, client, mock_pm):
        agent = MagicMock()
        agent.agent_id = "a1"
        agent.status.value = "completed"
        agent.prompt = "test"
        agent.model = None
        agent.session_id = None
        agent.started_at.isoformat.return_value = "2024-01-01T00:00:00"
        agent.completed_at = None
        agent.parent_id = None
        agent.children_ids = []
        agent.task_type = "generative"
        agent.reported_result = "done"
        agent.goal = None
        agent.goal_retries = 0
        agent.supervisor_retries = 0
        agent.max_goal_retries = None
        agent.exit_code = None
        agent.interactive = False
        agent.user_terminated = False
        agent.system_prompt = None
        agent.output_events = []
        agent.turn_markers = []
        agent.messages = []
        agent.workspace_path = None
        agent.label = None
        agent.plan_content = None
        agent.plan_file = None
        agent.supervisor = None
        agent.oom_retries = 0
        mock_pm.get_agent.return_value = agent
        resp = client.get("/api/agent/a1")
        assert resp.status_code == 200
        mock_pm.get_agent.return_value = None


# -------------------------------------------------------
# POST /api/agent — 请求体校验
# -------------------------------------------------------

class TestStartAgent:
    def test_missing_prompt_returns_422(self, client):
        resp = client.post("/api/agent", json={})
        assert resp.status_code == 422

    def test_missing_prompt_with_other_fields_returns_422(self, client):
        resp = client.post("/api/agent", json={"model": "claude-3"})
        assert resp.status_code == 422


# -------------------------------------------------------
# GET /api/models  →  {"models": [...]}
# -------------------------------------------------------

class TestListModels:
    def test_returns_models_list(self, client, mock_pm):
        mock_pm.list_models.return_value = []
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        assert isinstance(body["models"], list)


# -------------------------------------------------------
# GET /api/dag/templates  →  {"templates": [...]}
# -------------------------------------------------------

class TestDagTemplates:
    def test_returns_templates_list(self, client):
        resp = client.get("/api/dag/templates")
        assert resp.status_code == 200
        body = resp.json()
        assert "templates" in body
        assert isinstance(body["templates"], list)


# -------------------------------------------------------
# GET /api/workspaces  →  {"workspaces": [...]}
# -------------------------------------------------------

class TestListWorkspaces:
    def test_returns_workspaces_list(self, client):
        resp = client.get("/api/workspaces")
        assert resp.status_code == 200
        body = resp.json()
        assert "workspaces" in body
        assert isinstance(body["workspaces"], list)
