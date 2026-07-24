"""最小化测试 AgentOS 启动。"""
import sys, os, importlib.util
from pathlib import Path

_this_dir = Path(__file__).parent.parent
for pkg, loc in [("agent_os", _this_dir), ("agent_os.src", _this_dir / "src")]:
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(pkg, loc / "__init__.py", submodule_search_locations=[str(loc)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)

os.environ["AGENT_OS_BACKEND"] = "codebuddy-sdk"
from agent_os.src.core.agent_os import AgentOS

agent_os = AgentOS(project_root=os.getcwd(), cli_command="codebuddy", port=18420)
print("AgentOS created OK")
print("Models:", agent_os.list_models())

# 跑一个简单的 run
run_id = pm.start_run(
    prompt="Say 'E2E_OK' and nothing else.",
    model="claude-sonnet-4.6",
    system_prompt="Be concise.",
)
print(f"Run ID: {run_id}")

import time
for _ in range(60):
    run = pm.runs.get(run_id)
    if run and run.status.value != "running":
        break
    time.sleep(1)

run = pm.runs[run_id]
print(f"Status: {run.status.value}, exit_code: {run.exit_code}")
all_text = " ".join(
    e.get("text", "") for e in run.output_events
    if e.get("kind") in ("text", "text_delta")
)
print(f"Output: {all_text[:100]}")
assert "E2E_OK" in all_text, f"No E2E_OK: {all_text[:100]}"
print("[PASS]")
