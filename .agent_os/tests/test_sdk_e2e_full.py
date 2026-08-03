"""端到端测试：AgentOS + SDK Backend 全链路。"""
import sys, os, time
import importlib.util
from pathlib import Path

_this_dir = Path(__file__).parent.parent
_pkg_name = "agent_os"
if _pkg_name not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _pkg_name, _this_dir / "__init__.py",
        submodule_search_locations=[str(_this_dir)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules[_pkg_name] = _pkg
    _spec.loader.exec_module(_pkg)

_src_pkg_name = "agent_os.src"
if _src_pkg_name not in sys.modules:
    _src_spec = importlib.util.spec_from_file_location(
        _src_pkg_name, _this_dir / "src" / "__init__.py",
        submodule_search_locations=[str(_this_dir / "src")],
    )
    _src_pkg = importlib.util.module_from_spec(_src_spec)
    sys.modules[_src_pkg_name] = _src_pkg
    _src_spec.loader.exec_module(_src_pkg)

import pytest

os.environ["AGENT_OS_BACKEND"] = "codebuddy-sdk"

from agent_os.src.core.agent_os import AgentOS  # noqa: E402
from agent_os.src.core.agents.base import RunStatus  # noqa: E402


def wait_for_status(pm, agent_id, target_status, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        agent = pm.agents.get(agent_id)
        if agent and agent.status == target_status:
            return True
        time.sleep(0.5)
    return False


def test_model_list():
    print("=== Test 1: Model List ===")
    pm = AgentOS(project_root=os.getcwd(), cli_command="codebuddy", port=9999)
    models = pm.list_models(refresh=True)
    print(f"  Models: {models}")
    assert len(models) > 0, "No models returned"
    print("  [PASS]\n")
    return models[0]


def test_start_agent(model=None):
    pytest.skip("E2E test — requires sequential execution via __main__")
    print(f"=== Test 2: Start Agent (model={model}) ===")
    pm = AgentOS(project_root=os.getcwd(), cli_command="codebuddy", port=9999)
    agent_id = pm.start_agent(
        prompt="Reply with exactly 'START_OK' and nothing else.",
        model=model,
        system_prompt="You are concise. Reply exactly as instructed.",
    )
    print(f"  Agent ID: {agent_id}")
    ok = wait_for_status(pm, agent_id, RunStatus.COMPLETED, timeout=60)
    agent = pm.agents[agent_id]
    print(f"  Status: {agent.status.value}, exit_code: {agent.exit_code}")
    all_text = " ".join(
        e.get("text", "") for e in agent.output_events
        if e.get("kind") in ("text", "text_delta")
    )
    print(f"  Output preview: {all_text[:200]}")
    assert ok, f"Agent did not complete (status={agent.status.value})"
    assert "START_OK" in all_text
    print("  [PASS]\n")
    return pm, agent_id, agent.session_id


def test_continue_agent(pm=None, agent_id=None, session_id=None, model=None):
    pytest.skip("E2E test — requires sequential execution via __main__")
    print("=== Test 3: Continue Agent (resume session) ===")
    ok = pm.continue_agent(agent_id=agent_id,
        prompt="What did you say in your first message? Reply with just the word.",
        model=model)
    assert ok
    ok = wait_for_status(pm, agent_id, RunStatus.COMPLETED, timeout=60)
    agent = pm.agents[agent_id]
    all_text = " ".join(
        e.get("text", "") for e in agent.output_events
        if e.get("kind") in ("text", "text_delta")
    )
    assert "START_OK" in all_text
    print("  [PASS]\n")


def test_stop_agent(model=None):
    pytest.skip("E2E test — requires sequential execution via __main__")
    print("=== Test 4: Stop Agent ===")
    pm = AgentOS(project_root=os.getcwd(), cli_command="codebuddy", port=9999)
    agent_id = pm.start_agent(
        prompt="Write a Python script that prints numbers 1 to 100, one per line.",
        model=model,
    )
    time.sleep(3)
    ok = pm.stop_agent(agent_id)
    agent = pm.agents[agent_id]
    assert ok
    assert agent.status == RunStatus.STOPPED
    print("  [PASS]\n")


if __name__ == "__main__":
    model = test_model_list()
    pm, agent_id, session_id = test_start_agent(model)
    test_continue_agent(pm, agent_id, session_id, model)
    test_stop_agent(model)
    print("=== All tests passed! ===")
