"""Agent 底层接口测试 — 验证 AgentBackend 协议的一致性。

测试覆盖：
1. Native (CLI) 后端 — 需要 codebuddy CLI 已安装并登录
2. CodeBuddy SDK 后端 — 需要 codebuddy-agent-sdk 已安装

运行：
    python .agent_os/tests/test_agent_backend.py              # 全部测试
    python .agent_os/tests/test_agent_backend.py --backend native  # 只测 CLI
    python .agent_os/tests/test_agent_backend.py --backend codebuddy-sdk  # 只测 SDK
"""
import sys, os, json, time, argparse
from pathlib import Path

# 包注册
import importlib.util
_this_dir = Path(__file__).parent.parent
for pkg, loc in [("agent_os", _this_dir), ("agent_os.src", _this_dir / "src")]:
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg, loc / "__init__.py", submodule_search_locations=[str(loc)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)

from agent_os.src.agent.backend import (
    AgentBackend, get_backend, SessionHandle,
    NativeBackend, CodeBuddySDKBackend,
)

# ============================================================================
# 测试工具
# ============================================================================

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = []

    def ok(self, msg: str = ""):
        self.passed += 1
        print(f"  [PASS] {msg}" if msg else f"  [PASS]")

    def fail(self, msg: str):
        self.failed += 1
        self.errors.append(msg)
        print(f"  [FAIL] {msg}")

    def skip(self, msg: str):
        self.skipped += 1
        print(f"  [SKIP] {msg}")

    def check(self, condition, msg: str):
        if condition:
            self.ok(msg)
        else:
            self.fail(msg)

    def summary(self):
        total = self.passed + self.failed + self.skipped
        print(f"\n  Results: {self.passed} passed, {self.failed} failed, {self.skipped} skipped ({total} total)")
        if self.errors:
            print("  Failures:")
            for e in self.errors:
                print(f"    - {e}")
        return self.failed == 0


# ============================================================================
# 协议一致性测试（所有后端通用）
# ============================================================================

def test_protocol(backend: AgentBackend, r: TestResult):
    """验证后端实现了 AgentBackend 协议的所有方法。"""
    r.check(hasattr(backend, "list_models"), "has list_models")
    r.check(hasattr(backend, "launch"), "has launch")
    r.check(hasattr(backend, "stream"), "has stream")
    r.check(hasattr(backend, "get_session_path"), "has get_session_path")
    r.check(hasattr(backend, "evaluate"), "has evaluate")

    r.check(callable(backend.list_models), "list_models is callable")
    r.check(callable(backend.launch), "launch is callable")
    r.check(callable(backend.stream), "stream is callable")
    r.check(callable(backend.get_session_path), "get_session_path is callable")
    r.check(callable(backend.evaluate), "evaluate is callable")


def test_list_models(backend: AgentBackend, r: TestResult):
    """模型列表。"""
    models = backend.list_models()
    r.check(isinstance(models, list), f"list_models returns list")
    r.check(len(models) > 0, f"list_models not empty ({len(models)} models)")
    for m in models:
        r.check(isinstance(m, str), f"model '{m}' is str")


def test_launch_returns_handle(backend: AgentBackend, r: TestResult):
    """launch() 返回 SessionHandle。"""
    try:
        handle = backend.launch(
            prompt="Say 'OK' and nothing else.",
            system_prompt="You are concise. Reply exactly as instructed.",
            cwd=os.getcwd(),
        )
    except Exception as e:
        r.fail(f"launch() raised: {e}")
        return

    r.check(isinstance(handle, SessionHandle), f"returns SessionHandle")
    r.check(hasattr(handle, "poll"), "handle has poll()")
    r.check(hasattr(handle, "wait"), "handle has wait()")
    r.check(hasattr(handle, "terminate"), "handle has terminate()")
    r.check(hasattr(handle, "returncode"), "handle has returncode")
    r.check(hasattr(handle, "pid"), "handle has pid")

    # 清理
    handle.terminate()
    handle.wait(timeout=5)


def test_stream_events(backend: AgentBackend, r: TestResult):
    """stream() 产出结构化事件。"""
    handle = backend.launch(
        prompt="Say 'hello world' and nothing else.",
        system_prompt="Be concise.",
        cwd=os.getcwd(),
    )

    events = []
    timeout = time.time() + 60
    try:
        for ev in backend.stream(handle):
            if time.time() > timeout:
                r.fail("stream timeout")
                handle.terminate()
                break
            events.append(ev)
            r.check("kind" in ev, f"event has 'kind': {ev.get('kind')}")
            r.check(isinstance(ev.get("kind"), str), f"kind is str: {ev.get('kind')}")
    except Exception as e:
        r.fail(f"stream() raised: {e}")
        handle.terminate()

    handle.wait(timeout=5)

    kinds = [e.get("kind") for e in events]
    r.check(len(events) > 0, f"got {len(events)} events")
    r.check("text" in kinds or "text_delta" in kinds, f"has text/text_delta events: {set(kinds)}")

    # 检查是否有实际文本输出
    all_text = " ".join(
        e.get("text", "") for e in events
        if e.get("kind") in ("text", "text_delta")
    )
    r.check("hello" in all_text.lower(), f"output contains 'hello': {all_text[:80]}")


def test_session_resume(backend: AgentBackend, r: TestResult):
    """会话恢复：第二轮能记住第一轮的内容。"""
    # Round 1
    handle1 = backend.launch(
        prompt="Remember this number: 42. Reply 'OK, remembered 42.'",
        system_prompt="Be concise.",
        cwd=os.getcwd(),
    )
    session_id = None
    for ev in backend.stream(handle1):
        sid = ev.get("session_id", "")
        if sid:
            session_id = sid
    handle1.wait(timeout=5)
    r.check(session_id is not None, f"got session_id from round 1: {str(session_id)[:13] if session_id else 'NONE'}")

    if not session_id:
        r.skip("no session_id, cannot test resume")
        return

    # Round 2: resume
    handle2 = backend.launch(
        prompt="What number did I ask you to remember? Reply with just the number.",
        resume_session=session_id,
        cwd=os.getcwd(),
    )
    all_text = ""
    for ev in backend.stream(handle2):
        t = ev.get("text", "")
        if t:
            all_text += t
    handle2.wait(timeout=5)

    r.check("42" in all_text, f"resume remembered 42: {all_text[:100]}")


def test_terminate(backend: AgentBackend, r: TestResult):
    """terminate() 能中断运行中的 agent。"""
    handle = backend.launch(
        prompt="Write a Python script that prints numbers 1 to 1000, one per line.",
        cwd=os.getcwd(),
    )
    time.sleep(2)
    handle.terminate()
    handle.wait(timeout=5)

    rc = handle.returncode
    r.check(rc is not None and rc != 0, f"terminated, returncode={rc} (expected non-zero)")


def test_evaluate(backend: AgentBackend, r: TestResult):
    """evaluate() 语义评估。"""
    # 明确成功的评估
    is_met, reason = backend.evaluate(
        goal="Say hello",
        context="The agent said: hello world",
        cwd=os.getcwd(),
    )
    r.check(is_met is True, f"evaluate 'hello' → met (reason: {reason[:80]})")

    # 明确失败的评估
    is_met2, reason2 = backend.evaluate(
        goal="Write a sorting algorithm",
        context="The agent said: hello world",
        cwd=os.getcwd(),
    )
    r.check(is_met2 is False, f"evaluate 'sorting' → not met (reason: {reason2[:80]})")


def test_session_file(backend: AgentBackend, r: TestResult):
    """get_session_path() 返回路径或 None。"""
    # 先跑一个 session 拿到 session_id
    handle = backend.launch(
        prompt="Say hi",
        system_prompt="Be concise.",
        cwd=os.getcwd(),
    )
    session_id = None
    for ev in backend.stream(handle):
        sid = ev.get("session_id", "")
        if sid:
            session_id = sid
    handle.wait(timeout=5)

    if not session_id:
        r.skip("no session_id")
        return

    path = backend.get_session_path(session_id, cwd=os.getcwd())
    if path:
        r.check(os.path.exists(path), f"session file exists: {path}")
        # 验证文件内容
        with open(path, encoding="utf-8") as f:
            content = f.read()
        r.check(len(content) > 0, f"session file not empty ({len(content)} bytes)")
    else:
        r.fail(f"get_session_path returned None for session_id={session_id}")


# ============================================================================
# 测试套件
# ============================================================================

ALL_TESTS = [
    ("protocol", test_protocol),
    ("list_models", test_list_models),
    ("launch", test_launch_returns_handle),
    ("stream", test_stream_events),
    ("session_resume", test_session_resume),
    ("terminate", test_terminate),
    ("evaluate", test_evaluate),
    ("session_file", test_session_file),
]


def run_backend_tests(backend_type: str, backend: AgentBackend):
    print(f"\n{'='*60}")
    print(f"  Backend: {backend_type} ({type(backend).__name__})")
    print(f"{'='*60}")

    all_ok = True
    for name, fn in ALL_TESTS:
        r = TestResult(name)
        print(f"\n--- {name} ---")
        try:
            fn(backend, r)
        except Exception as e:
            r.fail(f"unexpected error: {e}")
            import traceback
            traceback.print_exc()
        if not r.summary():
            all_ok = False

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Agent Backend Tests")
    parser.add_argument("--backend", choices=["native", "codebuddy-sdk", "all"],
                        default="all", help="Which backend to test")
    args = parser.parse_args()

    backends_to_test = []
    if args.backend in ("all", "native"):
        try:
            native = get_backend("native", cli_command="codebuddy")
            backends_to_test.append(("native", native))
        except Exception as e:
            print(f"[SKIP] native backend: {e}")

    if args.backend in ("all", "codebuddy-sdk"):
        try:
            sdk = get_backend("codebuddy-sdk")
            backends_to_test.append(("codebuddy-sdk", sdk))
        except Exception as e:
            print(f"[SKIP] codebuddy-sdk backend: {e}")

    if not backends_to_test:
        print("No backends available to test.")
        return

    all_ok = True
    for name, backend in backends_to_test:
        if not run_backend_tests(name, backend):
            all_ok = False

    print(f"\n{'='*60}")
    print(f"  OVERALL: {'PASS' if all_ok else 'FAIL'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
