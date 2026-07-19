"""P1 测试：FastAPI 路由层（dashboard/app.py）。

由于 dashboard/app.py 使用 `from ..src.process_manager import ...` 相对导入，
测试时需要先把整个包结构注入到 sys.modules，再 import app。
涵盖：
  - GET /api/runs     — 返回 {"runs": [...]}
  - GET /api/run/{id} — 已存在 200，不存在 404
  - POST /api/run     — 缺少 prompt 返回 422
  - GET /api/models   — 返回 {"models": [...]}
  - GET /api/dag/templates — 返回 {"templates": [...]}
  - GET /api/workspaces — 返回 {"workspaces": [...]}
"""
import sys
import os
import types
from unittest.mock import MagicMock
import pytest

# ---- 路径 ----
AGENT_OS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _build_package_in_sys_modules():
    """把 .agent_os/ 目录作为 agent_os 包注入 sys.modules（只做一次）。"""
    if "agent_os" in sys.modules and isinstance(sys.modules["agent_os"], types.ModuleType):
        return

    pkg = types.ModuleType("agent_os")
    pkg.__path__ = [AGENT_OS_DIR]
    pkg.__package__ = "agent_os"
    pkg.__spec__ = None
    sys.modules["agent_os"] = pkg

    mock_pm_mod = types.ModuleType("agent_os.process_manager")
    mock_pm_mod.ProcessManager = MagicMock
    mock_pm_mod.RunStatus = MagicMock()
    sys.modules["agent_os.process_manager"] = mock_pm_mod
    sys.modules["process_manager"] = mock_pm_mod

    mock_rec = types.ModuleType("agent_os.recorder")
    mock_rec.Recorder = MagicMock
    sys.modules["agent_os.recorder"] = mock_rec
    sys.modules["recorder"] = mock_rec

    dash_pkg = types.ModuleType("agent_os.dashboard")
    dash_pkg.__path__ = [os.path.join(AGENT_OS_DIR, "dashboard")]
    dash_pkg.__package__ = "agent_os.dashboard"
    sys.modules["agent_os.dashboard"] = dash_pkg


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mock_pm():
    m = MagicMock()
    m.runs = {}
    # 给需要序列化的方法返回可 JSON 化的值
    m.list_runs.return_value = []
    m.list_models.return_value = []
    m.get_workspace_path = MagicMock(return_value=None)
    _app_mod.set_process_manager(m)
    return m


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/runs  →  {"runs": [...]}
# ---------------------------------------------------------------------------

class TestListRuns:
    def test_empty_runs_returns_dict_with_runs_key(self, client, mock_pm):
        mock_pm.runs = {}
        mock_pm.list_runs.return_value = []
        resp = client.get("/api/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert "runs" in body
        assert isinstance(body["runs"], list)


# ---------------------------------------------------------------------------
# GET /api/run/{run_id}
# ---------------------------------------------------------------------------

class TestGetRun:
    def test_missing_run_returns_404(self, client, mock_pm):
        # get_run 返回 None 触发 404 分支
        mock_pm.get_run.return_value = None
        resp = client.get("/api/run/nonexistent-xyz")
        assert resp.status_code == 404

    def test_existing_run_returns_200(self, client, mock_pm):
        run = MagicMock()
        run.run_id = "r1"
        run.status.value = "completed"
        run.prompt = "test"
        run.model = None
        run.session_id = None
        run.started_at.isoformat.return_value = "2024-01-01T00:00:00"
        run.completed_at = None
        run.parent_run_id = None
        run.children_run_ids = []
        run.step_id = None
        run.task_type = "generative"
        run.reported_result = "done"
        run.goal = None
        run.goal_retries = 0
        run.exit_code = None
        run.interactive = False
        run.user_terminated = False
        run.system_prompt = None
        run.output_lines = []
        run.output_events = []
        run.turn_markers = []
        run.messages = []
        run.workspace_path = None
        mock_pm.get_run.return_value = run
        mock_pm.MAX_GOAL_RETRIES = 3  # 必须是可 JSON 序列化的整型
        resp = client.get("/api/run/r1")
        assert resp.status_code == 200
        mock_pm.get_run.return_value = None


# ---------------------------------------------------------------------------
# POST /api/run — 请求体校验（422 由 Pydantic 校验触发，不依赖 pm）
# ---------------------------------------------------------------------------

class TestStartRun:
    def test_missing_prompt_returns_422(self, client):
        resp = client.post("/api/run", json={})
        assert resp.status_code == 422

    def test_missing_prompt_with_other_fields_returns_422(self, client):
        resp = client.post("/api/run", json={"model": "claude-3"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/models  →  {"models": [...]}
# ---------------------------------------------------------------------------

class TestListModels:
    def test_returns_dict_with_models_key(self, client, mock_pm):
        mock_pm.list_models.return_value = []
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        assert isinstance(body["models"], list)


# ---------------------------------------------------------------------------
# GET /api/dag/templates  →  {"templates": [...]}
# ---------------------------------------------------------------------------

class TestDagTemplates:
    def test_returns_dict_with_templates_key(self, client):
        resp = client.get("/api/dag/templates")
        assert resp.status_code == 200
        body = resp.json()
        assert "templates" in body
        assert isinstance(body["templates"], list)


# ---------------------------------------------------------------------------
# GET /api/workspaces  →  {"workspaces": [...]}
# ---------------------------------------------------------------------------

class TestListWorkspaces:
    def test_returns_dict_with_workspaces_key(self, client):
        resp = client.get("/api/workspaces")
        assert resp.status_code == 200
        body = resp.json()
        assert "workspaces" in body
        assert isinstance(body["workspaces"], list)
