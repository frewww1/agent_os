"""端到端测试：AgentOS + SDK Backend 全链路。

验证：
1. 模型列表获取
2. start_run + _read_output 流式解析
3. session resume (continue_run)
4. terminate 停止
"""
import sys, os, json, time, threading

# 模拟 main.py 的包注册
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

import asyncio
os.environ["AGENT_OS_BACKEND"] = "codebuddy-sdk"

from agent_os.src.core.agent_os import AgentOS
from agent_os.src.core.models import RunStatus


def wait_for_status(pm, run_id, target_status, timeout=60):
    """轮询等待 run 达到目标状态。"""
    start = time.time()
    while time.time() - start < timeout:
        run = pm.runs.get(run_id)
        if run and run.status == target_status:
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


def test_start_run(model):
    print(f"=== Test 2: Start Run (model={model}) ===")
    pm = AgentOS(project_root=os.getcwd(), cli_command="codebuddy", port=9999)

    run_id = pm.start_run(
        prompt="Reply with exactly 'START_OK' and nothing else.",
        model=model,
        system_prompt="You are concise. Reply exactly as instructed.",
    )
    print(f"  Run ID: {run_id}")

    ok = wait_for_status(pm, run_id, RunStatus.COMPLETED, timeout=60)
    run = pm.runs[run_id]
    print(f"  Status: {run.status.value}, exit_code: {run.exit_code}")

    # 检查输出（从 output_events 提取文本）
    all_text = " ".join(
        e.get("text", "") for e in run.output_events
        if e.get("kind") in ("text", "text_delta")
    )
    print(f"  Output preview: {all_text[:200]}")

    assert ok, f"Run did not complete in time (status={run.status.value})"
    assert "START_OK" in all_text, f"Expected START_OK in output"
    print(f"  [PASS]\n")
    return pm, run_id, run.session_id


def test_continue_run(pm, run_id, session_id, model):
    print(f"=== Test 3: Continue Run (resume session) ===")

    ok = pm.continue_run(
        run_id=run_id,
        prompt="What did you say in your first message? Reply with just the word.",
        model=model,
    )
    assert ok, "continue_run returned False"
    print(f"  Continue started")

    ok = wait_for_status(pm, run_id, RunStatus.COMPLETED, timeout=60)
    run = pm.runs[run_id]
    print(f"  Status: {run.status.value}")

    all_text = " ".join(
        e.get("text", "") for e in run.output_events
        if e.get("kind") in ("text", "text_delta")
    )
    print(f"  Output preview: {all_text[200:400]}")
    assert "START_OK" in all_text, "Agent should remember previous context"
    print(f"  [PASS]\n")


def test_stop_run(model):
    print(f"=== Test 4: Stop Run ===")
    pm = AgentOS(project_root=os.getcwd(), cli_command="codebuddy", port=9999)

    run_id = pm.start_run(
        prompt="Write a Python script that prints numbers 1 to 100, one per line.",
        model=model,
    )
    print(f"  Run ID: {run_id}")

    # 等 3 秒然后终止
    time.sleep(3)
    ok = pm.stop_run(run_id)
    run = pm.runs[run_id]
    print(f"  Stopped: {ok}, Status: {run.status.value}")
    assert ok, "stop_run returned False"
    assert run.status == RunStatus.STOPPED, f"Expected STOPPED, got {run.status.value}"
    print(f"  [PASS]\n")


if __name__ == "__main__":
    model = test_model_list()
    pm, run_id, session_id = test_start_run(model)
    test_continue_run(pm, run_id, session_id, model)
    test_stop_run(model)
    print("=== All tests passed! ===")
