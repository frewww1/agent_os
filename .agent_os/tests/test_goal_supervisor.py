"""Goal & Supervisor 核心逻辑测试。

直接测试 ProcessManager 的 goal/supervisor 方法，
使用 mock backend 避免实际 SDK 调用。

    python .agent_os/tests/test_goal_supervisor.py
"""
import sys, os, importlib.util, json, time, threading, queue
from pathlib import Path

_this_dir = Path(__file__).parent.parent
for pkg, loc in [("agent_os", _this_dir), ("agent_os.src", _this_dir / "src")]:
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg, loc / "__init__.py", submodule_search_locations=[str(loc)]
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)

os.environ["AGENT_OS_BACKEND"] = "codebuddy-sdk"

from agent_os.src.core.process_manager import ProcessManager, RunStatus
from agent_os.src.core.models import RunInfo
from agent_os.src.agent.backend import SessionHandle, AgentBackend

passed = 0
failed = 0


def test(name, fn):
    global passed, failed
    print(f"\n{'='*50}")
    print(f"  {name}")
    print(f"{'='*50}")
    try:
        fn()
        passed += 1
        print(f"  [PASS] {name}")
    except Exception as e:
        failed += 1
        print(f"  [FAIL] {name}: {e}")
        import traceback
        traceback.print_exc()


# ============================================================================
# Mock Backend
# ============================================================================
class MockBackend(AgentBackend):
    """Mock backend 用于测试，不实际调用 CLI/SDK。"""

    def __init__(self):
        self._evaluate_responses = []  # [(is_met, reason), ...]
        self._launched_sessions = []   # [(prompt, model, session_id), ...]
        self._evaluate_calls = []      # [(goal, context), ...]

    def list_models(self) -> list[str]:
        return ["mock-model"]

    def launch(self, prompt, model=None, session_id=None,
               resume_session=None, system_prompt=None,
               cwd=None, env=None) -> SessionHandle:
        self._launched_sessions.append((prompt, model, session_id))
        q = queue.Queue()
        stop = threading.Event()

        def _run():
            # 模拟 agent 输出
            q.put({"kind": "text", "text": "Mock agent output: task done."})
            q.put({"kind": "result", "result": "Mock result"})
            stop.set()
            q.put(None)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        handle = SessionHandle(
            _stop_event=stop,
            session_id=session_id or "mock-session",
            pid=-1,
        )
        handle._events_queue = q
        handle._sdk_thread = t
        return handle

    def stream(self, handle):
        q = handle._events_queue
        stop = handle._stop_event
        if not q or not stop:
            return
        try:
            while True:
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    if stop.is_set():
                        break
                    continue
                if item is None:
                    break
                if isinstance(item, dict):
                    yield item
        finally:
            handle.returncode = 0

    def get_session_path(self, session_id, cwd=None):
        return None

    def evaluate(self, goal, context, cwd=None):
        self._evaluate_calls.append((goal, context))
        if self._evaluate_responses:
            return self._evaluate_responses.pop(0)
        return True, "mock: assumed met"


# ============================================================================
# Setup
# ============================================================================
mock = MockBackend()
pm = ProcessManager(project_root=os.getcwd(), cli_command="codebuddy", port=19599)
pm._backend = mock  # 注入 mock backend
pm.default_model = "mock-model"


# ============================================================================
# Test 1: Goal set and get
# ============================================================================
def test_goal_set_get():
    run_id = pm.start_run(
        prompt="Test prompt",
        goal="The agent must output SUCCESS",
    )
    assert run_id, "No run_id returned"
    run_info = pm.get_run(run_id)
    assert run_info.goal == "The agent must output SUCCESS"
    assert run_info.goal_retries == 0
    print(f"  Goal set OK: {run_info.goal}")

    # set_goal 更新
    pm.set_goal(run_id, "New goal", max_retries=7)
    run_info = pm.get_run(run_id)
    assert run_info.goal == "New goal"
    assert run_info.goal_retries == 0  # reset
    assert getattr(run_info, '_max_goal_retries', None) == 7
    print(f"  set_goal update OK: {run_info.goal}, max_retries=7")


# ============================================================================
# Test 2: Goal evaluation - met on first try
# ============================================================================
def test_goal_eval_met():
    mock._evaluate_responses = [(True, "YES\nThe agent successfully output SUCCESS.")]

    run_id = pm.start_run(
        prompt="Test prompt",
        goal="Output SUCCESS",
    )
    run_info = pm.get_run(run_id)

    # 模拟 _on_run_completed 的 goal 评估逻辑
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "SUCCESS"

    # 记录当前 evaluate_calls 数量
    calls_before = len(mock._evaluate_calls)

    # 直接调用 _evaluate_goal
    is_met, reason = pm._evaluate_goal(run_info)
    assert is_met, f"Should be met: {reason}"
    print(f"  Goal met: {reason[:100]}")

    # 验证 evaluate 被调用
    new_calls = mock._evaluate_calls[calls_before:]
    assert len(new_calls) >= 1, f"Expected at least 1 new call, got {len(new_calls)}"
    goal, context = new_calls[-1]  # 最后一个调用
    assert "Output SUCCESS" in goal
    assert "SUCCESS" in context
    print(f"  Evaluate called with goal='{goal}', context_len={len(context)}")


# ============================================================================
# Test 3: Goal evaluation - NOT met, retry
# ============================================================================
def test_goal_eval_not_met():
    mock._evaluate_responses = [(False, "NO\nThe agent did not output the required text.")]

    run_id = pm.start_run(
        prompt="Test prompt",
        goal="Output SPECIFIC_TEXT_12345",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Wrong output"
    # 需要设置 _loop 否则 continue_run 会失败
    import asyncio
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    is_met, reason = pm._evaluate_goal(run_info)
    assert not is_met, f"Should NOT be met: {reason}"
    print(f"  Goal NOT met: {reason[:100]}")


# ============================================================================
# Test 4: Goal evaluation - no goal (skip)
# ============================================================================
def test_goal_eval_no_goal():
    mock._evaluate_responses = [(True, "YES")]

    run_id = pm.start_run(prompt="Test prompt")  # no goal
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED

    calls_before = len(mock._evaluate_calls)
    is_met, reason = pm._evaluate_goal(run_info)
    assert is_met, "Should be met (no goal)"
    assert reason == "no goal"
    assert len(mock._evaluate_calls) == calls_before  # 没调用 evaluate
    print(f"  No goal → skip: {reason}")


# ============================================================================
# Test 5: Goal evaluation - empty context
# ============================================================================
def test_goal_eval_empty_context():
    run_id = pm.start_run(
        prompt="Test prompt",
        goal="Some goal",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = None
    run_info.reported_result = None

    is_met, reason = pm._evaluate_goal(run_info)
    assert is_met, "Should be met (empty context assumes met)"
    assert "no content" in reason or "assume met" in reason
    print(f"  Empty context → assume met: {reason}")


# ============================================================================
# Test 6: skip_goal
# ============================================================================
def test_skip_goal():
    run_id = pm.start_run(
        prompt="Test prompt",
        goal="Some goal",
    )
    run_info = pm.get_run(run_id)
    assert run_info.goal == "Some goal"
    assert run_info.goal_retries == 0

    pm.skip_goal(run_id)
    run_info = pm.get_run(run_id)
    # skip_goal 将 goal_retries 设为 max
    assert run_info.goal_retries >= pm.MAX_GOAL_RETRIES, \
        f"retries={run_info.goal_retries} should be >= {pm.MAX_GOAL_RETRIES}"
    print(f"  skip_goal OK: retries={run_info.goal_retries}")


# ============================================================================
# Test 7: max_goal_retries limit
# ============================================================================
def test_max_goal_retries():
    run_id = pm.start_run(
        prompt="Test prompt",
        goal="Some goal",
    )
    pm.set_goal(run_id, "Goal with custom retries", max_retries=2)
    run_info = pm.get_run(run_id)
    assert getattr(run_info, '_max_goal_retries', None) == 2
    print(f"  Custom max_retries=2 OK")

    # 模拟 retries 达到上限
    run_info.goal_retries = 2
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "anything"

    # 此时 goal_retries(2) >= max_retries(2)，不应该触发 evaluate
    # 这通常在 _on_run_completed 中检查
    max_r = getattr(run_info, '_max_goal_retries', pm.MAX_GOAL_RETRIES)
    should_eval = run_info.goal and run_info.goal_retries < max_r
    assert not should_eval, "Should NOT evaluate when retries >= max"
    print(f"  Retry limit check OK: retries={run_info.goal_retries} >= max={max_r}")


# ============================================================================
# Test 8: Supervisor mode - PASS
# ============================================================================
def test_supervisor_pass():
    # 设置 mock evaluate 返回 PASS
    mock._evaluate_responses = [(True, "YES")]

    run_id = pm.start_run(
        prompt="Test prompt for supervisor",
        goal="Some task",
        supervisor="You are a strict supervisor.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Task completed successfully."
    run_info.goal_retries = 0

    # 注入 mock 让 supervisor launch 返回 PASS
    # _run_supervisor 会调用 self._backend.launch() 启动 supervisor agent
    # mock backend 的 launch 返回的 session 会输出 "Mock agent output: task done."
    # supervisor 解析这个输出时不会匹配 PASS/CORRECTION/CONTINUE
    # 所以会走 CONTINUE 路径

    # 直接测试 _run_supervisor 的行为
    handled = pm._run_supervisor(run_info)
    # mock 输出 "Mock agent output: task done." 不匹配任何 supervisor 指令
    # 所以 supervisor 应该走 CONTINUE 路径，返回 False
    assert not handled, "Supervisor should not handle (CONTINUE path)"
    print(f"  Supervisor CONTINUE: handled={handled}")

    # 现在模拟 supervisor 返回 PASS
    # 重新 mock launch 来返回 PASS
    original_launch = mock.launch

    class PassSessionHandle:
        _events_queue = queue.Queue()
        _stop_event = threading.Event()
        _sdk_thread = None
        session_id = "supervisor-pass"
        pid = -1
        returncode = 0
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass

    def pass_launch(prompt, model=None, session_id=None, **kwargs):
        handle = PassSessionHandle()
        handle._events_queue.put({"kind": "text", "text": "PASS"})
        handle._events_queue.put(None)
        return handle

    mock.launch = pass_launch

    run_id2 = pm.start_run(
        prompt="Another test",
        goal="Task",
        supervisor="Check and reply PASS.",
    )
    run_info2 = pm.get_run(run_id2)
    run_info2.status = RunStatus.COMPLETED
    run_info2._fallback_result = "Done."
    run_info2.goal_retries = 0

    handled = pm._run_supervisor(run_info2)
    assert not handled, "PASS should return False (not handled, let normal flow continue)"
    # 验证 goal 被清空
    assert run_info2.goal is None, "Goal should be cleared after PASS"
    assert run_info2.goal_retries >= pm.MAX_GOAL_RETRIES, \
        f"Retries should be maxed after PASS: {run_info2.goal_retries}"
    print(f"  Supervisor PASS: goal cleared, retries={run_info2.goal_retries}")

    mock.launch = original_launch


# ============================================================================
# Test 9: Supervisor mode - CORRECTION
# ============================================================================
def test_supervisor_correction():
    import asyncio

    class CorrectionSessionHandle:
        _events_queue = queue.Queue()
        _stop_event = threading.Event()
        _sdk_thread = None
        session_id = "supervisor-corr"
        pid = -1
        returncode = 0
        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass

    original_launch = mock.launch
    def corr_launch(prompt, model=None, session_id=None, **kwargs):
        handle = CorrectionSessionHandle()
        handle._events_queue.put({
            "kind": "text",
            "text": "CORRECTION: The file content is wrong. Please write 'Hello World' instead of 'Hi'."
        })
        handle._events_queue.put(None)
        return handle

    mock.launch = corr_launch

    run_id = pm.start_run(
        prompt="Create greeting.txt",
        goal="Create greeting.txt with Hello World",
        supervisor="Check greeting.txt content.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Created greeting.txt with Hi"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    handled = pm._run_supervisor(run_info)
    assert handled, "CORRECTION should return True (handled, resume triggered)"
    print(f"  Supervisor CORRECTION: handled={handled}")

    # 验证 system event 被添加
    system_events = [e for e in run_info.output_events if e.get("kind") == "system"]
    correction_events = [e for e in system_events if "correction" in e.get("text", "").lower()]
    assert len(correction_events) > 0, "Should have correction system event"
    print(f"  Correction event: {correction_events[0]['text'][:100]}")

    mock.launch = original_launch


# ============================================================================
# Test 10: _build_work_context
# ============================================================================
def test_build_work_context():
    run_id = pm.start_run(prompt="Test context")
    run_info = pm.get_run(run_id)

    # 无产出时
    ctx = pm._build_work_context(run_info)
    assert ctx == "", f"Empty context should be empty: '{ctx}'"
    print(f"  Empty context OK: len={len(ctx)}")

    # 有 reported_result
    run_info.reported_result = "Task completed: file created."
    ctx = pm._build_work_context(run_info)
    assert "Task completed" in ctx
    assert "Final report" in ctx
    print(f"  Context with report: {ctx[:100]}")

    # 有 fallback_result
    run_info.reported_result = None
    run_info._fallback_result = "Fallback output"
    ctx = pm._build_work_context(run_info)
    assert "Fallback output" in ctx
    print(f"  Context with fallback: {ctx[:100]}")

    # 有 output_events
    run_info.add_event("text", text="Event text 1")
    run_info.add_event("text", text="Event text 2")
    ctx = pm._build_work_context(run_info)
    assert "Event text 1" in ctx
    assert "Work log" in ctx
    print(f"  Context with events: len={len(ctx)}")


# ============================================================================
# Test 11: _on_run_completed - goal auto-fill from DAG
# ============================================================================
def test_on_run_completed_goal_autofill():
    import asyncio

    # 创建临时 dag.json
    import tempfile
    tmpdir = tempfile.mkdtemp()
    dag_path = os.path.join(tmpdir, "dag.json")
    dag = {
        "name": "test",
        "steps": [
            {"id": "auto_step", "name": "Auto", "prompt": "test",
             "depends_on": [], "type": "generative",
             "goal": "Agent must create output.txt"},
        ]
    }
    with open(dag_path, "w") as f:
        json.dump(dag, f)

    run_id = pm.start_run(
        prompt="Test",
        goal=None,  # 没有 goal
    )
    run_info = pm.get_run(run_id)
    run_info.step_id = "auto_step"
    run_info.workspace_path = tmpdir
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    # 直接测试 auto-fill 逻辑
    # 从 _on_run_completed 中提取 auto-fill 代码
    if not run_info.goal and run_info.step_id and run_info.workspace_path:
        from agent_os.src.core import dag_planner as dp
        dag_loaded = dp.load_dag(run_info.workspace_path)
        for s in dag_loaded.get("steps", []):
            if s.get("id") == run_info.step_id:
                goal_from_dag = s.get("goal") or ""
                if goal_from_dag:
                    run_info.goal = goal_from_dag
                break

    assert run_info.goal == "Agent must create output.txt", \
        f"Goal should be auto-filled, got: {run_info.goal}"
    print(f"  Auto-fill OK: goal='{run_info.goal}'")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================================
# Test 12: DAG step goal via spawn_children
# ============================================================================
def test_spawn_children_with_goal():
    import asyncio

    # 先创建父 run
    parent_id = pm.start_run(prompt="Parent task")
    parent = pm.get_run(parent_id)
    object.__setattr__(parent, '_loop', asyncio.get_event_loop())

    # spawn 子 agent（模拟 DAG planner 的行为）
    tasks = [{
        "prompt": "Execute step 1: create hello.py",
        "agent_name": "step1-agent",
        "step_id": "step1",
        "goal": "Create hello.py that prints Hello World",
        "type": "generative",
    }]

    result = pm.spawn_children(parent_id, parent.session_id, tasks)
    assert result["child_count"] == 1
    child_id = result["child_run_ids"][0]
    print(f"  Spawned child: {child_id}")

    child = pm.get_run(child_id)
    assert child.goal == "Create hello.py that prints Hello World", \
        f"Child goal: {child.goal}"
    assert child.step_id == "step1"
    assert child.parent_run_id == parent_id
    print(f"  Child goal OK: {child.goal}")
    print(f"  Child step_id OK: {child.step_id}")


# ============================================================================
# Test 12b: DAG step supervisor via spawn_children
# ============================================================================
def test_spawn_children_with_supervisor():
    """子 agent 继承 DAG step 的 supervisor。"""
    import asyncio

    parent_id = pm.start_run(prompt="Parent task")
    parent = pm.get_run(parent_id)
    object.__setattr__(parent, '_loop', asyncio.get_event_loop())

    tasks = [{
        "prompt": "Execute step 1: review code",
        "agent_name": "reviewer-agent",
        "step_id": "step_review",
        "supervisor": "Check code quality and report issues.",
        "type": "generative",
    }]

    result = pm.spawn_children(parent_id, parent.session_id, tasks)
    child_id = result["child_run_ids"][0]
    child = pm.get_run(child_id)
    assert child.supervisor == "Check code quality and report issues.", \
        f"Child supervisor: {child.supervisor}"
    print(f"  Child supervisor OK: {child.supervisor[:50]}...")


# ============================================================================
# Test 13: Supervisor field stored in RunInfo
# ============================================================================
def test_supervisor_stored():
    run_id = pm.start_run(
        prompt="Test",
        supervisor="You are a code reviewer. Check for bugs.",
    )
    run_info = pm.get_run(run_id)
    assert run_info.supervisor == "You are a code reviewer. Check for bugs."
    print(f"  Supervisor stored: {run_info.supervisor[:50]}...")

    # supervisor 为 None 时
    run_id2 = pm.start_run(prompt="Test without supervisor")
    run_info2 = pm.get_run(run_id2)
    assert run_info2.supervisor is None
    print(f"  No supervisor: {run_info2.supervisor}")


# ============================================================================
# Test 14: _on_run_completed — supervisor 优先于 goal 评估
# ============================================================================
def test_on_run_completed_supervisor_first():
    """supervisor 先执行；如果 supervisor 返回 CORRECTION 则 return，不走 goal 评估。"""
    import asyncio

    # 构造 supervisor 返回 CORRECTION 的 mock
    original_launch = mock.launch
    launch_count = [0]

    def mixed_launch(prompt, model=None, session_id=None, **kwargs):
        launch_count[0] += 1
        handle = object.__new__(SessionHandle)
        handle._events_queue = queue.Queue()
        handle._stop_event = threading.Event()
        handle._sdk_thread = None
        handle.session_id = session_id or "mixed"
        handle.pid = -1
        handle.returncode = 0
        # 第一次 launch（supervisor）：返回 CORRECTION
        handle._events_queue.put({
            "kind": "text",
            "text": "CORRECTION: Fix the output to include REQUIRED_TEXT.",
        })
        handle._events_queue.put(None)
        return handle

    mock.launch = mixed_launch

    run_id = pm.start_run(
        prompt="Test",
        goal="Must output REQUIRED_TEXT",
        supervisor="Check the output.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Wrong output"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    # 记录 goal 评估是否被调用
    eval_calls_before = len(mock._evaluate_calls)

    # 直接调用 _on_run_completed
    pm._on_run_completed(run_info)

    # supervisor CORRECTION 后应该 return，不调用 goal 评估
    assert len(mock._evaluate_calls) == eval_calls_before, \
        "Goal eval should NOT be called after supervisor CORRECTION"
    # 应该有 system event
    system_events = [e for e in run_info.output_events if e.get("kind") == "system"]
    correction_msgs = [e for e in system_events if "correction" in e.get("text", "").lower()]
    assert len(correction_msgs) > 0, "Should have correction message"
    print(f"  Supervisor blocked goal eval: eval_calls unchanged")

    mock.launch = original_launch


# ============================================================================
# Test 15: _on_run_completed — supervisor PASS 后继续 goal 评估
# ============================================================================
def test_on_run_completed_supervisor_then_goal():
    """supervisor 返回 PASS 后，goal_retries 被设为 MAX，goal 被清空，不再走 goal 评估。"""
    import asyncio

    original_launch = mock.launch

    def pass_launch_fn(prompt, model=None, session_id=None, **kwargs):
        handle = object.__new__(SessionHandle)
        handle._events_queue = queue.Queue()
        handle._stop_event = threading.Event()
        handle._sdk_thread = None
        handle.session_id = session_id or "pass-goal"
        handle.pid = -1
        handle.returncode = 0
        handle._events_queue.put({"kind": "text", "text": "PASS"})
        handle._events_queue.put(None)
        return handle

    mock.launch = pass_launch_fn

    run_id = pm.start_run(
        prompt="Test",
        goal="Output SUCCESS",
        supervisor="Check output.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "SUCCESS output"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    eval_calls_before = len(mock._evaluate_calls)
    pm._on_run_completed(run_info)

    # 有 supervisor 时，直接 return，不走 goal 评估
    assert len(mock._evaluate_calls) == eval_calls_before, \
        "Goal eval should NOT be called when supervisor exists"
    # _run_supervisor PASS 内部清空了 goal
    assert run_info.goal is None, f"Goal should be cleared by supervisor PASS: {run_info.goal}"
    print(f"  Supervisor exists → goal eval skipped, goal cleared by supervisor")

    mock.launch = original_launch


# ============================================================================
# Test 16: _on_run_completed — goal NOT met triggers continue_run
# ============================================================================
def test_on_run_completed_goal_retry():
    """goal 未达成时，_on_run_completed 调用 continue_run 注入反馈。"""
    import asyncio

    mock._evaluate_responses = [(False, "NO\nMissing REQUIRED_OUTPUT.")]

    run_id = pm.start_run(
        prompt="Test",
        goal="Must output REQUIRED_OUTPUT",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Wrong"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    # 验证 retries 递增
    retries_before = run_info.goal_retries
    pm._on_run_completed(run_info)

    assert run_info.goal_retries > retries_before, \
        f"goal_retries should increment: {run_info.goal_retries} > {retries_before}"
    # 应该有 system event 提示 goal not met
    system_events = [e for e in run_info.output_events if e.get("kind") == "system"]
    goal_msgs = [e for e in system_events if "Goal not met" in e.get("text", "")]
    assert len(goal_msgs) > 0, "Should have 'Goal not met' system event"
    print(f"  Goal NOT met → retries={run_info.goal_retries}, system event added")


# ============================================================================
# Test 17: _on_run_completed — goal met clears state
# ============================================================================
def test_on_run_completed_goal_met_clears():
    """goal 达成后，retries 被 maxed，goal 被清空。"""
    import asyncio

    mock._evaluate_responses = [(True, "YES\nAll requirements met.")]

    run_id = pm.start_run(
        prompt="Test",
        goal="Output anything",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    pm._on_run_completed(run_info)

    assert run_info.goal is None, f"Goal should be None after met: {run_info.goal}"
    max_r = getattr(run_info, '_max_goal_retries', pm.MAX_GOAL_RETRIES)
    assert run_info.goal_retries >= max_r, \
        f"Retries should be maxed: {run_info.goal_retries} >= {max_r}"
    print(f"  Goal met → cleared, retries={run_info.goal_retries}")


# ============================================================================
# Test 18: interactive agent skips supervisor and goal
# ============================================================================
def test_interactive_skips_supervisor_goal():
    """interactive agent 不触发 supervisor 和 goal 评估。"""
    import asyncio

    eval_calls_before = len(mock._evaluate_calls)

    run_id = pm.start_run(
        prompt="Test",
        goal="Some goal",
        supervisor="Check it.",
        interactive=True,
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    pm._on_run_completed(run_info)

    # 不应该触发 supervisor 或 goal 评估
    assert len(mock._evaluate_calls) == eval_calls_before, \
        "Interactive agent should not trigger goal eval"
    # goal 不应该被清空
    assert run_info.goal == "Some goal", "Interactive agent goal should remain"
    print(f"  Interactive agent skipped supervisor+goal")


# ============================================================================
# Test 19: _run_supervisor — empty context returns False
# ============================================================================
def test_supervisor_empty_context():
    """supervisor 在 context 为空时直接返回 False，不启动 agent。"""
    run_id = pm.start_run(
        prompt="Test",
        supervisor="Check.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    # 清空所有产出
    run_info._fallback_result = None
    run_info.reported_result = None
    run_info.output_events.clear()

    launch_count_before = len(mock._launched_sessions)
    handled = pm._run_supervisor(run_info)
    assert not handled, "Empty context should return False"
    # 不应该启动新的 session
    assert len(mock._launched_sessions) == launch_count_before, \
        "Should not launch supervisor for empty context"
    print(f"  Empty context → supervisor skipped")


# ============================================================================
# Test 20: _run_supervisor — unknown response treated as CONTINUE
# ============================================================================
def test_supervisor_unknown_response():
    """supervisor 返回未知格式时走 CONTINUE 路径。"""
    original_launch = mock.launch

    def unknown_launch(prompt, model=None, session_id=None, **kwargs):
        handle = object.__new__(SessionHandle)
        handle._events_queue = queue.Queue()
        handle._stop_event = threading.Event()
        handle._sdk_thread = None
        handle.session_id = session_id or "unknown"
        handle.pid = -1
        handle.returncode = 0
        # 返回一个不匹配 PASS/CORRECTION/CONTINUE 的响应
        handle._events_queue.put({
            "kind": "text",
            "text": "The task looks good, but needs more work on edge cases.",
        })
        handle._events_queue.put(None)
        return handle

    mock.launch = unknown_launch

    run_id = pm.start_run(
        prompt="Test",
        goal="Task",
        supervisor="Check.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    run_info.goal_retries = 0

    handled = pm._run_supervisor(run_info)
    assert not handled, "Unknown response should be treated as CONTINUE"
    # 应该有 CONTINUE system event
    system_events = [e for e in run_info.output_events if e.get("kind") == "system"]
    continue_msgs = [e for e in system_events if "continue" in e.get("text", "").lower()]
    assert len(continue_msgs) > 0, "Should have 'continue' system event"
    print(f"  Unknown response → CONTINUE")

    mock.launch = original_launch


# ============================================================================
# Test 21: _build_work_context — messages field
# ============================================================================
def test_build_work_context_messages():
    """验证 messages 字段被包含在上下文中。"""
    run_id = pm.start_run(prompt="Test context")
    run_info = pm.get_run(run_id)
    run_info._fallback_result = "Some output"

    # 添加 messages
    run_info.messages.append({"msg": "Progress: step 1 done"})
    run_info.messages.append({"msg": "Progress: step 2 done"})

    ctx = pm._build_work_context(run_info)
    assert "Progress: step 1 done" in ctx
    assert "Progress messages" in ctx
    print(f"  Messages in context: {ctx[:150]}")


# ============================================================================
# Test 22: _build_work_context — 12000 char truncation
# ============================================================================
def test_build_work_context_truncation():
    """验证上下文在 12000 字符处截断。"""
    run_id = pm.start_run(prompt="Test context")
    run_info = pm.get_run(run_id)

    # 构造超长输出
    long_text = "X" * 15000
    run_info._fallback_result = long_text

    ctx = pm._build_work_context(run_info)
    assert len(ctx) <= 12000, f"Context should be truncated to 12000: {len(ctx)}"
    print(f"  Truncation OK: {len(ctx)} <= 12000")


# ============================================================================
# Test 23: set_goal / skip_goal — invalid run_id
# ============================================================================
def test_set_skip_goal_invalid_id():
    """对不存在的 run_id 操作返回 False。"""
    assert pm.set_goal("nonexistent", "goal") is False
    assert pm.skip_goal("nonexistent") is False
    print(f"  Invalid run_id → False for both")


# ============================================================================
# Test 24: set_goal — empty goal string
# ============================================================================
def test_set_goal_empty():
    """设置空字符串 goal。"""
    run_id = pm.start_run(prompt="Test", goal="Initial goal")
    run_info = pm.get_run(run_id)

    pm.set_goal(run_id, "")
    run_info = pm.get_run(run_id)
    assert run_info.goal == "", f"Empty goal should be allowed: {run_info.goal!r}"
    assert run_info.goal_retries == 0
    print(f"  Empty goal set OK")


# ============================================================================
# Test 25: continue_run — os source preserves goal_retries
# ============================================================================
def test_continue_run_os_preserves_retries():
    """source='os' 时 continue_run 不重置 goal_retries。"""
    import asyncio

    run_id = pm.start_run(prompt="Test", goal="Some goal")
    run_info = pm.get_run(run_id)
    run_info.goal_retries = 3
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())
    # 绕过 session poll 检查：把 _session 设为 None 让 continue_run 走 resume 逻辑
    object.__setattr__(run_info, '_session', None)

    pm.continue_run(run_id, "Feedback", source="os")
    assert run_info.goal_retries == 3, \
        f"os source should preserve retries: {run_info.goal_retries}"
    print(f"  os source preserved retries={run_info.goal_retries}")


# ============================================================================
# Test 26: continue_run — user source resets goal_retries
# ============================================================================
def test_continue_run_user_resets_retries():
    """source='user' 时 continue_run 重置 goal_retries=0。"""
    import asyncio

    run_id = pm.start_run(prompt="Test", goal="Some goal")
    run_info = pm.get_run(run_id)
    run_info.goal_retries = 3
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())
    object.__setattr__(run_info, '_session', None)

    pm.continue_run(run_id, "User feedback", source="user")
    assert run_info.goal_retries == 0, \
        f"user source should reset retries: {run_info.goal_retries}"
    print(f"  user source reset retries to 0")


# ============================================================================
# Test 27: continue_run — runtime goal override
# ============================================================================
def test_continue_run_goal_override():
    """continue_run 允许运行时覆盖 goal。"""
    import asyncio

    run_id = pm.start_run(prompt="Test", goal="Original goal")
    run_info = pm.get_run(run_id)
    run_info.goal_retries = 2
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())
    object.__setattr__(run_info, '_session', None)

    pm.continue_run(run_id, "Prompt", source="os", goal="Overridden goal")
    assert run_info.goal == "Overridden goal", f"Goal not overridden: {run_info.goal}"
    assert run_info.goal_retries == 0, "Retries should reset on goal override"
    print(f"  Goal override OK: {run_info.goal}")

    # goal="" 时清空（None 表示不改变）
    object.__setattr__(run_info, '_session', None)
    pm.continue_run(run_id, "Prompt2", source="os", goal="")
    assert run_info.goal is None, f"Goal should be cleared, got: {run_info.goal!r}"
    print(f"  Goal cleared OK")


# ============================================================================
# Test 28: max_goal_retries affects supervisor limit too
# ============================================================================
def test_supervisor_no_retry_limit():
    """Supervisor 不受 max_goal_retries 限制，可以无限审查。"""
    import asyncio

    original_launch = mock.launch

    def corr_launch(prompt, model=None, session_id=None, **kwargs):
        mock._launched_sessions.append((prompt, model, session_id))  # 记录
        handle = object.__new__(SessionHandle)
        handle._events_queue = queue.Queue()
        handle._stop_event = threading.Event()
        handle._sdk_thread = None
        handle.session_id = session_id or "corr-nolimit"
        handle.pid = -1
        handle.returncode = 0
        handle._events_queue.put({
            "kind": "text",
            "text": "CORRECTION: Fix the output.",
        })
        handle._events_queue.put(None)
        return handle

    mock.launch = corr_launch

    run_id = pm.start_run(
        prompt="Test",
        goal="Task",
        supervisor="Check.",
    )
    pm.set_goal(run_id, "Task", max_retries=2)  # goal 最多 2 次
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    # 记录 launch 次数
    launch_count_before = len(mock._launched_sessions)

    # supervisor 触发 3 次（超过 goal 的 max_retries=2）
    for i in range(3):
        run_info.status = RunStatus.COMPLETED
        pm._on_run_completed(run_info)

    # 验证 supervisor launch 被调用了 3 次
    new_launches = len(mock._launched_sessions) - launch_count_before
    assert new_launches >= 3, \
        f"Supervisor should have no retry limit, got {new_launches} launches"
    print(f"  Supervisor triggered {new_launches} times (no limit)")

    mock.launch = original_launch


# ============================================================================
# Test 29: _on_run_completed — FAILED status does not trigger
# ============================================================================
def test_on_run_completed_failed_skips():
    """FAILED 状态的 run 不触发 supervisor/goal 评估。"""
    import asyncio

    eval_before = len(mock._evaluate_calls)

    run_id = pm.start_run(
        prompt="Test",
        goal="Task",
        supervisor="Check.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.FAILED  # 不是 COMPLETED
    run_info._fallback_result = "Error"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    pm._on_run_completed(run_info)

    # 不应该触发任何评估
    assert len(mock._evaluate_calls) == eval_before, "FAILED should not trigger eval"
    assert run_info.goal_retries == 0, "Retries should not increment on FAILED"
    print(f"  FAILED status skipped supervisor+goal")


# ============================================================================
# Test 30: DAG goal auto-fill — step_id not in dag.json
# ============================================================================
def test_dag_goal_autofill_no_match():
    """step_id 在 dag.json 中不存在时，不崩溃、不设置 goal。"""
    import asyncio
    import tempfile, shutil

    tmpdir = tempfile.mkdtemp()
    dag_path = os.path.join(tmpdir, "dag.json")
    dag = {
        "name": "test",
        "steps": [
            {"id": "other_step", "name": "Other", "prompt": "test",
             "depends_on": [], "type": "generative",
             "goal": "Some goal"},
        ]
    }
    with open(dag_path, "w") as f:
        json.dump(dag, f)

    run_id = pm.start_run(prompt="Test", goal=None)
    run_info = pm.get_run(run_id)
    run_info.step_id = "non_existent_step"  # 不在 dag 中
    run_info.workspace_path = tmpdir
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    pm._on_run_completed(run_info)
    # goal 应该保持 None
    assert run_info.goal is None, f"Goal should remain None: {run_info.goal}"
    print(f"  No match in dag → goal stays None")

    shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================================
# Test 31: DAG goal auto-fill — step has no goal field
# ============================================================================
def test_dag_goal_autofill_no_goal_field():
    """step 在 dag.json 中没有 goal 字段时，不设置 goal。"""
    import asyncio
    import tempfile, shutil

    tmpdir = tempfile.mkdtemp()
    dag_path = os.path.join(tmpdir, "dag.json")
    dag = {
        "name": "test",
        "steps": [
            {"id": "no_goal_step", "name": "NoGoal", "prompt": "test",
             "depends_on": [], "type": "generative"},
            # 没有 goal 字段
        ]
    }
    with open(dag_path, "w") as f:
        json.dump(dag, f)

    run_id = pm.start_run(prompt="Test", goal=None)
    run_info = pm.get_run(run_id)
    run_info.step_id = "no_goal_step"
    run_info.workspace_path = tmpdir
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    pm._on_run_completed(run_info)
    assert run_info.goal is None, f"Goal should remain None when step has no goal: {run_info.goal}"
    print(f"  Step without goal field → goal stays None")

    shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================================
# Test 32: DAG goal auto-fill — dag.json load error
# ============================================================================
def test_dag_goal_autofill_load_error():
    """dag.json 损坏/不存在时不崩溃。"""
    import asyncio
    import tempfile, shutil

    tmpdir = tempfile.mkdtemp()
    # 不创建 dag.json

    run_id = pm.start_run(prompt="Test", goal=None)
    run_info = pm.get_run(run_id)
    run_info.step_id = "some_step"
    run_info.workspace_path = tmpdir
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "Done"
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    # 不应抛出异常
    pm._on_run_completed(run_info)
    assert run_info.goal is None
    print(f"  Missing dag.json → no crash, goal stays None")

    shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================================
# Test 33: Supervisor session reuse (跨轮复用同一会话)
# ============================================================================
def test_supervisor_session_reuse():
    """supervisor 多轮审查复用同一会话，让 supervisor 看到历史审查记录。"""
    import asyncio

    original_launch = mock.launch
    launch_sessions = []  # 记录每次 launch 的 session_id 和 resume_session

    def tracking_launch(prompt, model=None, session_id=None,
                         resume_session=None, system_prompt=None, **kwargs):
        launch_sessions.append({
            "session_id": session_id,
            "resume_session": resume_session,
            "prompt_head": prompt[:60],
            "system_prompt": system_prompt or "",
        })
        handle = object.__new__(SessionHandle)
        handle._events_queue = queue.Queue()
        handle._stop_event = threading.Event()
        handle._sdk_thread = None
        handle.session_id = session_id or "tracked"
        handle.pid = -1
        handle.returncode = 0
        # 返回 CONTINUE 避免触发 continue_run 干扰
        handle._events_queue.put({
            "kind": "text",
            "text": "CONTINUE",
        })
        handle._events_queue.put(None)
        return handle

    mock.launch = tracking_launch

    run_id = pm.start_run(
        prompt="Task",
        goal="Complete the task",
        supervisor="You are a supervisor.",
    )
    run_info = pm.get_run(run_id)
    run_info.status = RunStatus.COMPLETED
    run_info._fallback_result = "First attempt"
    run_info.goal_retries = 0
    object.__setattr__(run_info, '_loop', asyncio.get_event_loop())

    # 直接调用 _run_supervisor 两次（避免 _on_run_completed 触发 continue_run 干扰）
    pm._run_supervisor(run_info)
    pm._run_supervisor(run_info)

    # 验证：前两次是 _run_supervisor 直接调用，可能有额外的 _on_run_completed 触发
    print(f"  Total launches: {len(launch_sessions)}")
    for i, ls in enumerate(launch_sessions):
        sid = ls['session_id'][:13] if ls['session_id'] else 'None'
        res = ls['resume_session'][:13] if ls['resume_session'] else 'None'
        print(f"    [{i}] sid={sid}... resume={res}... prompt={ls['prompt_head']}")

    assert len(launch_sessions) >= 3, f"Expected at least 3 launches (1 agent + 2 supervisor), got {len(launch_sessions)}"

    # [0] 是 agent launch（start_run），[1] 和 [2] 是 supervisor launch
    first = launch_sessions[1]
    second = launch_sessions[2]

    assert first["session_id"] is not None, "First launch should have session_id"
    assert first["resume_session"] is None, "First launch should NOT have resume_session"
    assert second["session_id"] == first["session_id"], \
        f"Second launch should use same session_id: {second['session_id']} != {first['session_id']}"
    assert second["resume_session"] == first["session_id"], \
        f"Second launch should resume: {second['resume_session']} != {first['session_id']}"

    # 验证 _supervisor_session_id 被保存
    assert getattr(run_info, '_supervisor_session_id', None) == first["session_id"], \
        "RunInfo should store _supervisor_session_id"

    # 验证用户的 supervisor 提示词被放到了 system_prompt 中
    assert "You are a supervisor." in first["system_prompt"], \
        f"User supervisor prompt should be in system_prompt: {first['system_prompt'][:80]}"
    assert "strict supervisor" in first["system_prompt"], \
        "System prompt should contain supervisor role description"
    # 验证 prompt 中不再包含用户的 supervisor 提示词
    assert "You are a supervisor." not in first["prompt_head"], \
        f"User supervisor prompt should NOT be in prompt: {first['prompt_head']}"

    print(f"  Round 1: session_id={first['session_id'][:13]}...")
    print(f"  Round 2: resume_session={second['resume_session'][:13]}... (same session)")
    print(f"  system_prompt contains user's supervisor instructions")

    mock.launch = original_launch


# ============================================================================
# Main
# ============================================================================
def main():
    print(f"\n{'#'*60}")
    print(f"  Agent OS - Goal & Supervisor Logic Tests")
    print(f"{'#'*60}")
    print(f"  Using MockBackend (no real SDK calls)")

    # 单元级别：单个方法测试
    test("Goal set/get", test_goal_set_get)
    test("Goal evaluation - met", test_goal_eval_met)
    test("Goal evaluation - NOT met", test_goal_eval_not_met)
    test("Goal evaluation - no goal", test_goal_eval_no_goal)
    test("Goal evaluation - empty context", test_goal_eval_empty_context)
    test("skip_goal API", test_skip_goal)
    test("max_goal_retries limit", test_max_goal_retries)
    test("Supervisor - CONTINUE", test_supervisor_pass)
    test("Supervisor - CORRECTION", test_supervisor_correction)
    test("Supervisor - empty context skip", test_supervisor_empty_context)
    test("Supervisor - unknown response", test_supervisor_unknown_response)
    test("_build_work_context", test_build_work_context)
    test("_build_work_context - messages", test_build_work_context_messages)
    test("_build_work_context - truncation", test_build_work_context_truncation)

    # 集成级别：_on_run_completed 完整流程
    test("_on_run_completed: supervisor before goal", test_on_run_completed_supervisor_first)
    test("_on_run_completed: supervisor PASS → goal eval", test_on_run_completed_supervisor_then_goal)
    test("_on_run_completed: goal NOT met → retry", test_on_run_completed_goal_retry)
    test("_on_run_completed: goal met → clear", test_on_run_completed_goal_met_clears)
    test("_on_run_completed: interactive skips", test_interactive_skips_supervisor_goal)

    # DAG 相关
    test("Goal auto-fill from DAG", test_on_run_completed_goal_autofill)
    test("Goal auto-fill: no match", test_dag_goal_autofill_no_match)
    test("Goal auto-fill: no goal field", test_dag_goal_autofill_no_goal_field)
    test("Goal auto-fill: load error", test_dag_goal_autofill_load_error)
    test("spawn_children with goal", test_spawn_children_with_goal)
    test("spawn_children with supervisor", test_spawn_children_with_supervisor)

    # API 边界
    test("Supervisor field stored", test_supervisor_stored)
    test("set_goal/skip_goal invalid id", test_set_skip_goal_invalid_id)
    test("set_goal empty string", test_set_goal_empty)
    test("continue_run os preserves retries", test_continue_run_os_preserves_retries)
    test("continue_run user resets retries", test_continue_run_user_resets_retries)
    test("continue_run goal override", test_continue_run_goal_override)

    # 边界条件
    test("supervisor no retry limit", test_supervisor_no_retry_limit)
    test("FAILED status skips supervisor+goal", test_on_run_completed_failed_skips)
    test("supervisor session reuse", test_supervisor_session_reuse)

    print(f"\n{'#'*60}")
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print(f"{'#'*60}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
