"""Agent 后端：协议 + CLI/SDK 实现 + 工厂。"""
import json as _json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from typing import Iterator, Protocol, runtime_checkable

from .cli_resolver import parse_models_from_cli_inner, resolve_node_cli
from .cli_resolver import read_models_cache_file, write_models_cache_file
from ..utils import cwd_to_session_key

logger = logging.getLogger("agent_os")


# ============================================================
# 会话句柄
# ============================================================

@runtime_checkable
class SessionLike(Protocol):
    """会话句柄协议 — Popen 和 SDKHandle 都满足。"""
    @property
    def returncode(self) -> int | None: ...
    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...


class SDKHandle:
    """SDK 模式会话句柄。"""
    def __init__(self, session_id: str = ""):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._returncode: int | None = None
        self._events_queue: queue.Queue = queue.Queue()
        self.session_id = session_id
        self.pid = -1

    @property
    def returncode(self) -> int | None: return self._returncode
    @returncode.setter
    def returncode(self, value): self._returncode = value

    def poll(self) -> int | None:
        if self._thread and not self._thread.is_alive():
            return self._returncode or 0
        return None

    def wait(self, timeout: float | None = None) -> int:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return self._returncode if self._returncode is not None else -1

    def terminate(self) -> None:
        self._stop.set()
        self._returncode = -15


# ============================================================
# AgentBackend 协议
# ============================================================

@runtime_checkable
class AgentBackend(Protocol):
    def list_models(self) -> list[str]: ...
    def launch(self, prompt, model=None, session_id=None, resume_session=None,
               system_prompt=None, cwd=None, env=None) -> SessionLike: ...
    def stream(self, handle: SessionLike) -> Iterator[dict]: ...
    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None: ...


# ============================================================
# BaseAgentBackend
# ============================================================

class BaseAgentBackend:
    FALLBACK_MODELS = [
        "claude-sonnet-4.6", "claude-sonnet-4.5", "claude-opus-4.5",
        "deepseek-v4-pro", "deepseek-v4-flash", "gpt-5.1",
    ]

    def list_models(self) -> list[str]:
        models = read_models_cache_file()
        if models:
            return models
        models = self._discover_models()
        if models:
            write_models_cache_file(models)
            return models
        return self.FALLBACK_MODELS

    def _discover_models(self) -> list[str]:
        return []

    def stream(self, handle) -> Iterator[dict]:
        from .stream_parser import parse_stream_json_events
        stdout = getattr(handle, 'stdout', None)
        if not stdout:
            return
        for line in iter(stdout.readline, ""):
            if not line:
                break
            line = line.rstrip("\n\r")
            if not line:
                continue
            line = line.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
            for ev in parse_stream_json_events(line):
                yield ev

    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None:
        return _locate_cli_session_jsonl(session_id, cwd)


# ============================================================
# NativeBackend
# ============================================================

class NativeBackend(BaseAgentBackend):
    def __init__(self, cli_command: str = "codebuddy"):
        self.cli_command = cli_command
        self.cli_prefix = self._resolve_cli(cli_command)
        logger.info(f"NativeBackend: cli={cli_command}")

    def _discover_models(self) -> list[str]:
        return parse_models_from_cli_inner(self.cli_prefix)

    def launch(self, prompt, model=None, session_id=None, resume_session=None,
               system_prompt=None, cwd=None, env=None) -> subprocess.Popen:
        cmd = list(self.cli_prefix) + [
            "-p", prompt, "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--include-partial-messages", "--verbose",
        ]
        if model:
            cmd.extend(["--model", model])
        if resume_session:
            cmd.extend(["--resume", resume_session])
        elif session_id:
            cmd.extend(["--session-id", session_id])
        if system_prompt:
            spf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", delete=False, encoding="utf-8",
                dir=cwd or ".", prefix="agent_os_sp_")
            spf.write(system_prompt)
            spf.close()
            cmd.extend(["--system-prompt-file", spf.name])
            if not hasattr(self, '_sp_cleanup_files'):
                self._sp_cleanup_files = []
            self._sp_cleanup_files.append(spf.name)

        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd or ".", bufsize=1, encoding="utf-8", errors="replace", env=env,
        )
        process.session_id = session_id or resume_session or ""
        return process

    @staticmethod
    def _resolve_cli(cli_command: str) -> list[str]:
        resolved = shutil.which(cli_command)
        return resolve_node_cli(resolved if resolved else cli_command)


# ============================================================
# SDKBackend
# ============================================================

class SDKBackend(BaseAgentBackend):
    def launch(self, prompt, model=None, session_id=None, resume_session=None,
               system_prompt=None, cwd=None, env=None) -> SDKHandle:
        handle = SDKHandle(session_id=session_id or "")

        def _run():
            try:
                self._call_sdk(handle=handle, prompt=prompt, model=model or "",
                               session_id=session_id, resume_session=resume_session,
                               system_prompt=system_prompt, cwd=cwd, env=env,
                               stop=handle._stop)
            except Exception as e:
                logger.exception(f"SDK backend error: {e}")
                self._emit_event(handle, "error", error=str(e))
            finally:
                handle._stop.set()
                handle._events_queue.put(None)

        t = threading.Thread(target=_run, daemon=True, name=f"sdk-{session_id or '?'}")
        handle._thread = t
        t.start()
        return handle

    def stream(self, handle: SDKHandle) -> Iterator[dict]:
        q = handle._events_queue
        stop = handle._stop
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
                elif isinstance(item, str):
                    try:
                        obj = _json.loads(item)
                        if isinstance(obj, dict) and "kind" in obj:
                            yield obj
                        elif isinstance(obj, dict) and "type" in obj:
                            from .stream_parser import parse_stream_json_events
                            for ev in parse_stream_json_events(item):
                                yield ev
                    except Exception:
                        yield {"kind": "raw", "text": str(item)[:500]}
        finally:
            handle.returncode = 0

    def _emit_event(self, handle: SDKHandle, kind: str, **payload):
        handle._events_queue.put({"kind": kind, **payload})

    def _call_sdk(self, handle, prompt, model, session_id, resume_session,
                  system_prompt, cwd, env, stop):
        raise NotImplementedError("Subclass must implement _call_sdk()")


# ============================================================
# 工厂
# ============================================================

_BACKEND_REGISTRY: dict[str, type] = {
    "native": NativeBackend,
    "sdk": SDKBackend,
    "codebuddy-sdk": None,  # 惰性导入，避免循环依赖
}


def _get_codebuddy_sdk_backend():
    from .codebuddy_sdk import CodeBuddySDKBackend
    return CodeBuddySDKBackend


def register_backend(name: str, backend_cls: type):
    _BACKEND_REGISTRY[name] = backend_cls


def get_backend(backend_type: str = "native", cli_command: str = "codebuddy", **kwargs) -> AgentBackend:
    if backend_type == "sdk":
        custom = os.environ.get("AGENT_OS_SDK_BACKEND", "")
        if custom:
            try:
                mod_path, cls_name = custom.rsplit(".", 1)
                mod = __import__(mod_path, fromlist=[cls_name])
                custom_cls = getattr(mod, cls_name)
                if issubclass(custom_cls, SDKBackend):
                    return custom_cls(**kwargs)
            except Exception as e:
                logger.warning(f"Failed to load custom SDK backend {custom}: {e}")

    cls = _BACKEND_REGISTRY.get(backend_type, NativeBackend)
    if cls is None and backend_type == "codebuddy-sdk":
        cls = _get_codebuddy_sdk_backend()
    if backend_type == "native":
        return cls(cli_command=cli_command, **kwargs)
    return cls(**kwargs)


# ============================================================
# 工具
# ============================================================

def _locate_cli_session_jsonl(session_id: str, cwd: str | None = None) -> str | None:
    home = os.path.expanduser("~")
    roots = []
    env_root = os.environ.get("AGENT_OS_CLI_HOME")
    if env_root and os.path.isdir(os.path.join(env_root, "projects")):
        roots.append(env_root)
    for name in [".codebuddy", ".claude"]:
        p = os.path.join(home, name)
        if os.path.isdir(os.path.join(p, "projects")):
            roots.append(p)
    key = cwd_to_session_key(cwd) if cwd else None
    filename = f"{session_id}.jsonl"
    for root in roots:
        projects = os.path.join(root, "projects")
        if key:
            path = os.path.join(projects, key, filename)
            if os.path.exists(path):
                return path
        try:
            for proj in os.listdir(projects):
                candidate = os.path.join(projects, proj, filename)
                if os.path.exists(candidate):
                    return candidate
        except OSError:
            continue
    return None
