import os, sys, json, importlib.util
from pathlib import Path

_this_dir = Path(__file__).parent.parent
for pkg, loc in [("agent_os", _this_dir), ("agent_os.src", _this_dir / "src")]:
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(pkg, loc / "__init__.py", submodule_search_locations=[str(loc)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)

os.environ["AGENT_OS_BACKEND"] = "codebuddy-sdk"
from agent_os.src.core.process_manager import ProcessManager

pm = ProcessManager(project_root=os.getcwd(), cli_command="codebuddy", port=9999)
run = pm.get_run("1ed2adbe")
if not run:
    print("Run not found in memory, checking sqlite...")
    # runs are persisted
    print("Available runs:", list(pm.runs.keys())[:10])
else:
    print("Status:", run.status.value)
    print("Workspace:", run.workspace_path)
    if run.workspace_path:
        dag_path = os.path.join(run.workspace_path, "dag.json")
        if os.path.exists(dag_path):
            with open(dag_path, encoding="utf-8") as f:
                dag = json.load(f)
            print("\ndag.json:")
            print(json.dumps(dag, indent=2, ensure_ascii=False)[:5000])
        else:
            print(f"\nNo dag.json at {dag_path}")
            # check workspace dir
            if os.path.isdir(run.workspace_path):
                print("Workspace files:", os.listdir(run.workspace_path)[:20])
    print("\nEvents count:", len(run.output_events))
    # show event kinds
    kinds = set(e.get("kind") for e in run.output_events)
    print("Event kinds:", kinds)
