"""Agent OS 后端适配器 — 统一 Agent 抽象层。

每种后端（CLI/SDK/第三方）实现 AgentBackend 协议。
ProcessManager 只通过此协议操作 agent，不感知底层实现。

核心方法：
- launch()         启动/继续会话，返回 SessionHandle
- stream()         流式事件迭代器（统一接口，CLI 读管道，SDK 直接 yield）
- get_session_path()  返回会话文件路径（用于 rewind/clear）
- list_models()    模型列表
- evaluate()       语义评估

内置后端：
- native:          直接 subprocess.Popen 启动 CLI（默认）
- codebuddy-sdk:   通过 codebuddy-agent-sdk 启动
- sdk:             通用 SDK 基类（通过 AGENT_OS_SDK_BACKEND 指定自定义类）
- omnigent:        通过 Omnigent Server HTTP API 启动

配置方式：
    AGENT_OS_BACKEND=native           # CLI 模式
    AGENT_OS_BACKEND=codebuddy-sdk    # SDK 模式
    AGENT_OS_BACKEND=sdk              # 自定义 SDK 类
    AGENT_OS_SDK_BACKEND=mypkg.MyBackend
"""
import asyncio
import json as _json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol, runtime_checkable

logger = logging.getLogger("agent_os")


# ============================================================================
# SessionHandle — 统一的会话句柄
# ============================================================================

@dataclass
class SessionHandle:
    """启动 agent 后返回的会话句柄。

    统一了 CLI 的 subprocess.Popen 和 SDK 的线程对象。
    ProcessManager 通过此句柄操作 agent，不区分底层实现。
    """

    # 进程/线程控制
    process: subprocess.Popen | None = None   # CLI 模式的子进程
    _stop_event: threading.Event | None = None  # SDK 模式的停止信号
    _sdk_thread: threading.Thread | None = None  # SDK 模式的后台线程
    _returncode: int | None = field(default=None, init=False)  # SDK 模式的退出码
    _events_queue: object = field(default=None, init=False)  # SDK 模式的事件队列

    # 会话信息
    session_id: str = ""
    pid: int = -1

    @property
    def returncode(self) -> int | None:
        if self.process:
            return self.process.returncode
        return self._returncode

    @returncode.setter
    def returncode(self, value: int | None):
        self._returncode = value

    def poll(self) -> int | None:
        if self.process:
            return self.process.poll()
        if self._sdk_thread and not self._sdk_thread.is_alive():
            return self._returncode or 0
        return None

    def wait(self, timeout: float | None = None) -> int:
        if self.process:
            return self.process.wait(timeout=timeout)
        if self._sdk_thread and self._sdk_thread.is_alive():
            self._sdk_thread.join(timeout=timeout)
        return self.returncode if self.returncode is not None else -1

    def terminate(self):
        if self.process:
            self.process.terminate()
        elif self._stop_event:
            self._stop_event.set()
            self._returncode = -15

    def kill(self):
        if self.process:
            self.process.kill()
        elif self._stop_event:
            self._stop_event.set()
            self._returncode = -9


# ============================================================================
# AgentBackend — 统一 Agent 抽象协议
# ============================================================================

@runtime_checkable
class AgentBackend(Protocol):
    """Agent 后端统一接口。"""

    def list_models(self) -> list[str]:
        """返回可用模型 ID 列表。"""
        ...

    def launch(self, prompt: str,
               model: str | None = None,
               session_id: str | None = None,
               resume_session: str | None = None,
               system_prompt: str | None = None,
               cwd: str | None = None,
               env: dict | None = None,
               ) -> SessionHandle:
        """启动 agent 会话，返回会话句柄。"""
        ...

    def stream(self, handle: SessionHandle) -> Iterator[dict]:
        """流式读取 agent 输出事件。

        返回迭代器，每次 yield 一个事件 dict。
        事件格式与 stream_parser 输出一致：
            {"kind": "text", "text": "..."}
            {"kind": "text_delta", "text": "..."}
            {"kind": "tool_use", "tool": "Bash", "summary": "..."}
            {"kind": "tool_result", "text": "..."}
            {"kind": "plan_pending", ...}
            {"kind": "raw", "text": "..."}
        """
        ...

    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None:
        """返回会话文件路径（jsonl），用于 rewind/clear 操作。

        返回 None 表示该后端不支持会话文件操作（如纯 SDK 模式）。
        """
        ...

    def evaluate(self, goal: str, context: str,
                 cwd: str | None = None,
                 ) -> tuple[bool, str]:
        """评估 agent 是否达成了 goal。返回 (is_met, reason)。"""
        ...


# ============================================================================
# BaseAgentBackend — 提供 stream/evaluate 默认实现
# ============================================================================

class BaseAgentBackend:
    """Agent 后端基类。

    子类必须实现: list_models(), launch()
    可选覆写: stream(), get_session_path(), evaluate()
    """

    def stream(self, handle: SessionHandle) -> Iterator[dict]:
        """默认实现：从 subprocess.stdout 逐行读取并解析。

        CLI 后端用此默认实现。SDK 后端应覆写为直接 yield 事件。
        """
        from ..core.stream_parser import parse_stream_json_events, extract_session_id

        if not handle.process or not handle.process.stdout:
            return

        class _FakeRunInfo:
            run_id = "eval"
            session_id = None
            reported_result = None
            _fallback_result = None

        fake_ri = _FakeRunInfo()

        for line in iter(handle.process.stdout.readline, ""):
            if not line:
                break
            line = line.rstrip("\n\r")
            if not line:
                continue
            line = line.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
            extract_session_id(fake_ri, line)
            for ev in parse_stream_json_events(line):
                yield ev

    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None:
        """默认实现：CLI 后端的 jsonl 文件路径。

        SDK 后端应覆写此方法（可能没有本地会话文件）。
        """
        return _locate_cli_session_jsonl(session_id, cwd)

    def evaluate(self, goal: str, context: str,
                 cwd: str | None = None,
                 ) -> tuple[bool, str]:
        if not goal or not context.strip():
            return True, "no goal or empty context"

        prompt = (
            f"Evaluate this task outcome. Reply ONLY with YES or NO on the first line, "
            f"then a brief reason on the second line.\n\n"
            f'Goal: {goal}\n\n{context[:12000]}\n\n'
            f'Did the agent achieve the goal? (YES/NO)'
        )

        try:
            handle = self.launch(
                prompt=prompt, model=None,
                system_prompt="You are a concise evaluator. Reply with YES or NO only.",
                cwd=cwd,
            )
            stdout_parts = []
            for ev in self.stream(handle):
                text = ev.get("text", "")
                if text:
                    stdout_parts.append(text)
            handle.wait()
            stdout = "".join(stdout_parts).strip()

            if not stdout:
                return True, "eval: empty output (assume met)"

            for line_text in stdout.upper().split("\n"):
                word = line_text.strip().lstrip("-*# ").strip()
                if not word:
                    continue
                if word.startswith("YES"):
                    rest = word[3:].strip()
                    if rest.lower().startswith(("or", "或")):
                        continue
                    return True, stdout[:300]
                if word.startswith("NO"):
                    rest = word[2:].strip()
                    if rest.lower().startswith(("or", "或")):
                        continue
                    return False, stdout[:300]

            if re.search(r'\bYES\b(?!\s*(or|OR|或))', stdout[:500]):
                return True, stdout[:300]
            if re.search(r'\bNO\b(?!\s*(or|OR|或))', stdout[:500]):
                return False, stdout[:300]

            return True, f"eval: unclear (assume met), head={stdout[:150]}"
        except Exception as e:
            logger.warning(f"evaluate failed: {e}")
            return True, f"eval error: {e}"


# ============================================================================
# Native 后端 — 直接 subprocess.Popen 启动 CLI
# ============================================================================

class NativeBackend(BaseAgentBackend):
    """原生 CLI 后端。

    配置：cli_config.json: {"cli": "codebuddy"}
    """

    def __init__(self, cli_command: str = "codebuddy"):
        self.cli_command = cli_command
        self.cli_prefix = self._resolve_cli(cli_command)
        logger.info(f"NativeBackend: cli={cli_command}, prefix={self.cli_prefix}")

    def list_models(self) -> list[str]:
        try:
            from ..core.cli_resolver import read_models_cache_file, parse_models_from_cli_inner
            models = read_models_cache_file()
            if not models:
                models = parse_models_from_cli_inner(self.cli_prefix)
            return models
        except ImportError:
            return _FALLBACK_MODELS

    def launch(self, prompt: str,
               model: str | None = None,
               session_id: str | None = None,
               resume_session: str | None = None,
               system_prompt: str | None = None,
               cwd: str | None = None,
               env: dict | None = None,
               ) -> SessionHandle:
        cmd = list(self.cli_prefix) + [
            "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if model:
            cmd.extend(["--model", model])
        if resume_session:
            cmd.extend(["--resume", resume_session])
        elif session_id:
            cmd.extend(["--session-id", session_id])
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=cwd or ".", bufsize=1,
            encoding="utf-8", errors="replace",
            env=env,
        )
        return SessionHandle(
            process=process,
            session_id=session_id or resume_session or "",
            pid=process.pid,
        )

    # stream() 和 get_session_path() 使用 BaseAgentBackend 默认实现

    @staticmethod
    def _resolve_cli(cli_command: str) -> list[str]:
        resolved = shutil.which(cli_command)
        cli_path = resolved if resolved else cli_command
        if not cli_path.lower().endswith('.cmd'):
            return [cli_path]
        target = _parse_cmd_shim(cli_path)
        if target is None or not _can_node_load(target):
            return [cli_path]
        return ['node', target]


# ============================================================================
# Omnigent 后端
# ============================================================================

class OmnigentBackend(BaseAgentBackend):
    """Omnigent 后端：通过 HTTP API 启动 agent。"""

    def __init__(self, base_url: str = "http://localhost:6767"):
        self.base_url = base_url
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from omnigent_client import OmnigentClient
                self._client = OmnigentClient(base_url=self.base_url)
            except ImportError:
                raise RuntimeError("omnigent-client not installed")
        return self._client

    def list_models(self) -> list[str]:
        try:
            import urllib.request
            req = urllib.request.Request(f"{self.base_url}/api/models")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read().decode()).get("models", [])
        except Exception as e:
            logger.warning(f"Omnigent list_models failed: {e}")
            return []

    def launch(self, prompt: str,
               model: str | None = None,
               session_id: str | None = None,
               resume_session: str | None = None,
               system_prompt: str | None = None,
               cwd: str | None = None,
               env: dict | None = None,
               ) -> SessionHandle:
        q: queue.Queue = queue.Queue()
        stop = threading.Event()

        def _run():
            try:
                client = self._get_client()
                for chunk in client.stream_chat(
                    prompt=prompt, model=model or "",
                    session_id=session_id or "",
                ):
                    if stop.is_set():
                        break
                    text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
                    q.put(text)
            except Exception as e:
                q.put(_json.dumps({"type": "error", "error": str(e)}) + "\n")
            finally:
                stop.set()
                q.put(None)

        t = threading.Thread(target=_run, daemon=True, name=f"omnigent-{session_id or '?'}")
        t.start()

        # 构建 FakeProcess 兼容的 stdout reader
        fake_stdout = _QueueLineReader(q, stop)

        return SessionHandle(
            process=_FakeProcessWrapper(fake_stdout, stop),
            _stop_event=stop,
            _sdk_thread=t,
            session_id=session_id or "",
            pid=-1,
        )

    def stream(self, handle: SessionHandle) -> Iterator[dict]:
        from ..core.stream_parser import parse_stream_json_events
        if handle.process and hasattr(handle.process, 'stdout'):
            for line in iter(handle.process.stdout.readline, ""):
                if not line:
                    break
                line = line.rstrip("\n\r")
                if not line:
                    continue
                for ev in parse_stream_json_events(line):
                    yield ev

    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None:
        return None  # Omnigent 没有本地会话文件


# ============================================================================
# SDK 后端基类
# ============================================================================

class SDKBackend(BaseAgentBackend):
    """通用 SDK 后端基类。

    子类覆写 _call_sdk() 接入任意 SDK。
    事件直接 yield，不经过 queue.Queue/FakeProcess/parse_stream_json_events。

    会话文件：SDK 底层调 CLI，jsonl 由 CLI 自动维护在 ~/.codebuddy/projects/ 下。
    """

    def list_models(self) -> list[str]:
        return _FALLBACK_MODELS

    def launch(self, prompt: str,
               model: str | None = None,
               session_id: str | None = None,
               resume_session: str | None = None,
               system_prompt: str | None = None,
               cwd: str | None = None,
               env: dict | None = None,
               ) -> SessionHandle:
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        handle = SessionHandle(
            _stop_event=stop,
            session_id=session_id or "",
            pid=-1,
        )
        handle._events_queue = q

        def _run():
            try:
                self._call_sdk(
                    handle=handle,
                    prompt=prompt, model=model or "",
                    session_id=session_id, resume_session=resume_session,
                    system_prompt=system_prompt,
                    cwd=cwd, env=env, stop=stop,
                )
            except Exception as e:
                logger.exception(f"SDK backend error: {e}")
                self._emit_event(handle, "error", error=str(e))
            finally:
                stop.set()
                q.put(None)

        t = threading.Thread(target=_run, daemon=True, name=f"sdk-{session_id or '?'}")
        t.start()
        handle._sdk_thread = t
        return handle

    def stream(self, handle: SessionHandle) -> Iterator[dict]:
        """SDK 后端：直接从 queue 读取事件 dict，不做 JSON 解析。"""
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
                elif isinstance(item, str):
                    try:
                        obj = _json.loads(item)
                        if isinstance(obj, dict) and "kind" in obj:
                            yield obj
                        elif isinstance(obj, dict) and "type" in obj:
                            from ..core.stream_parser import parse_stream_json_events
                            for ev in parse_stream_json_events(item):
                                yield ev
                    except Exception:
                        yield {"kind": "raw", "text": str(item)[:500]}
        finally:
            # stream 结束时标记退出码
            handle.returncode = 0

    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None:
        # SDK 底层也是 CLI，jsonl 在 ~/.codebuddy/projects/ 下
        return _locate_cli_session_jsonl(session_id, cwd)

    def _emit_event(self, handle: SessionHandle, kind: str, **payload):
        """子类调用此方法推送结构化事件。"""
        q = handle._events_queue
        if q:
            event = {"kind": kind, **payload}
            q.put(event)

    def _call_sdk(self, handle: SessionHandle, prompt: str, model: str,
                  session_id: str | None, resume_session: str | None,
                  system_prompt: str | None,
                  cwd: str | None, env: dict | None,
                  stop: threading.Event):
        """子类覆写，通过 self._emit_event(handle, kind, ...) 推送事件。"""
        raise NotImplementedError(
            "Subclass must implement _call_sdk(). Use self._emit_event(handle, kind, ...) to push events."
        )


# ============================================================================
# CodeBuddy SDK 后端
# ============================================================================

class CodeBuddySDKBackend(SDKBackend):
    """CodeBuddy Agent SDK 后端。

    使用 codebuddy-agent-sdk，SDK 消息直接转为事件 dict 推送，
    不走 queue.Queue → FakeProcess → JSON 序列化 → stream_parser 的弯路。
    """

    DEFAULT_SETTING_SOURCES: list[str] = []

    def _get_setting_sources(self) -> list[str]:
        raw = os.environ.get("AGENT_OS_SDK_SETTINGS", "")
        if raw:
            return [s.strip() for s in raw.split(",") if s.strip()]
        return list(self.DEFAULT_SETTING_SOURCES)

    def list_models(self) -> list[str]:
        try:
            from ..core.cli_resolver import read_models_cache_file, write_models_cache_file, parse_models_from_cli_inner
        except ImportError:
            read_models_cache_file = lambda: []
            write_models_cache_file = lambda m: None
            parse_models_from_cli_inner = lambda p: []

        models = read_models_cache_file()
        if models:
            return models
        models = self._read_codebuddy_models_json()
        if models:
            write_models_cache_file(models)
            return models
        models = parse_models_from_cli_inner(["codebuddy"])
        if models:
            write_models_cache_file(models)
            return models
        models = _FALLBACK_MODELS
        if models:
            write_models_cache_file(models)
        return models

    @staticmethod
    def _read_codebuddy_models_json() -> list[str]:
        candidates = [os.path.join(os.path.expanduser("~"), ".codebuddy", "models.json")]
        try:
            cwd_models = os.path.join(os.getcwd(), ".codebuddy", "models.json")
            if os.path.isfile(cwd_models):
                candidates.insert(0, cwd_models)
        except Exception:
            pass
        for path in candidates:
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8-sig") as f:
                        data = _json.load(f)
                    available = data.get("availableModels", [])
                    if isinstance(available, list) and available:
                        return [str(m) for m in available]
                    models_arr = data.get("models", [])
                    if isinstance(models_arr, list):
                        ids = [m.get("id", "") for m in models_arr if isinstance(m, dict) and m.get("id")]
                        if ids:
                            return ids
            except Exception as e:
                logger.debug(f"Failed to read models.json: {e}")
        return []

    def _call_sdk(self, handle: SessionHandle, prompt: str, model: str,
                  session_id: str | None, resume_session: str | None,
                  system_prompt: str | None,
                  cwd: str | None, env: dict | None,
                  stop: threading.Event):
        try:
            from codebuddy_agent_sdk import query, CodeBuddyAgentOptions
        except ImportError as e:
            self._emit_event(handle, "error", error=f"codebuddy-agent-sdk not installed: {e}")
            return

        opts = CodeBuddyAgentOptions(
            model=model or None,
            system_prompt=system_prompt or None,
            permission_mode="bypassPermissions",
            include_partial_messages=True,
            setting_sources=self._get_setting_sources(),
            cwd=cwd or None,
            env=dict(env) if env else {},
        )
        if resume_session:
            opts.resume = resume_session
        elif session_id:
            opts.session_id = session_id

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._run_sdk_query(handle, loop, prompt, opts, stop))
        finally:
            loop.close()

    async def _run_sdk_query(self, handle: SessionHandle, loop, prompt: str, opts, stop: threading.Event):
        from codebuddy_agent_sdk import query
        from codebuddy_agent_sdk import (
            AssistantMessage, SystemMessage, ResultMessage, StreamEvent,
            UserMessage, TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
        )
        import dataclasses

        try:
            async for msg in query(prompt=prompt, options=opts):
                if stop.is_set():
                    break

                if isinstance(msg, SystemMessage):
                    data = dataclasses.asdict(msg)
                    inner = dict(data.get("data", {}))
                    inner["subtype"] = data.get("subtype", "init")
                    inner["session_id"] = inner.get("session_id", "")
                    self._emit_event(handle, "system", **inner)
                    continue

                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            self._emit_event(handle, "text", text=block.text)
                        elif isinstance(block, ThinkingBlock):
                            self._emit_event(handle, "thinking",
                                thinking=block.thinking, signature=block.signature)
                        elif isinstance(block, ToolUseBlock):
                            self._emit_event(handle, "tool_use",
                                tool=block.name, id=block.id,
                                summary=_json.dumps(block.input, ensure_ascii=False)[:200],
                                input=block.input)
                        elif isinstance(block, ToolResultBlock):
                            text = block.content
                            if isinstance(text, list):
                                text = "\n".join(
                                    rc.get("text", "") for rc in text
                                    if isinstance(rc, dict) and rc.get("type") == "text"
                                )
                            self._emit_event(handle, "tool_result",
                                text=str(text)[:800] if text else "",
                                tool_use_id=block.tool_use_id,
                                is_error=block.is_error)
                    continue

                if isinstance(msg, StreamEvent):
                    data = dataclasses.asdict(msg)
                    inner = data.get("event", {})
                    inner_type = inner.get("type", "")
                    if inner_type in ("content_block_start", "content_block_delta"):
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            self._emit_event(handle, "text_delta", text=delta.get("text", ""))
                    continue

                if isinstance(msg, ResultMessage):
                    data = dataclasses.asdict(msg)
                    usage = dataclasses.asdict(msg.usage) if msg.usage else {}
                    self._emit_event(handle, "result",
                        subtype=data.get("subtype", ""),
                        is_error=data.get("is_error", False),
                        result=data.get("result", ""),
                        session_id=data.get("session_id", ""),
                        duration_ms=data.get("duration_ms", 0),
                        num_turns=data.get("num_turns", 0),
                        total_cost_usd=data.get("total_cost_usd"),
                        usage=usage)
                    continue

        except Exception as e:
            logger.exception(f"CodeBuddy SDK query error: {e}")
            self._emit_event(handle, "error", error=str(e))


# ============================================================================
# 后端注册表
# ============================================================================

_BACKEND_REGISTRY: dict[str, type] = {
    "native": NativeBackend,
    "omnigent": OmnigentBackend,
    "sdk": SDKBackend,
    "codebuddy-sdk": CodeBuddySDKBackend,
}

_FALLBACK_MODELS = [
    "claude-sonnet-4.6", "claude-sonnet-4.5", "claude-opus-4.5",
    "deepseek-v4-pro", "deepseek-v4-flash", "gpt-5.1",
]


def register_backend(name: str, backend_cls: type):
    """注册自定义后端。"""
    _BACKEND_REGISTRY[name] = backend_cls


def get_backend(backend_type: str = "native", cli_command: str = "codebuddy", **kwargs) -> AgentBackend:
    """根据配置创建后端实例。

    环境变量：
        AGENT_OS_BACKEND=native|codebuddy-sdk|sdk|omnigent
        AGENT_OS_SDK_BACKEND=my_package.MyCustomBackend
    """
    if backend_type == "sdk":
        custom = os.environ.get("AGENT_OS_SDK_BACKEND", "")
        if custom:
            try:
                mod_path, cls_name = custom.rsplit(".", 1)
                mod = __import__(mod_path, fromlist=[cls_name])
                custom_cls = getattr(mod, cls_name)
                if issubclass(custom_cls, SDKBackend):
                    return custom_cls(**kwargs)
                logger.warning(f"{custom} is not a SDKBackend subclass, falling back")
            except Exception as e:
                logger.warning(f"Failed to load custom SDK backend {custom}: {e}")

    cls = _BACKEND_REGISTRY.get(backend_type, NativeBackend)
    if backend_type == "native":
        return cls(cli_command=cli_command, **kwargs)
    return cls(**kwargs)


# ============================================================================
# 内部工具
# ============================================================================

class _FakeProcessWrapper:
    """兼容旧的 subprocess.Popen 接口，用于 Omnigent 等非 subprocess 后端。"""

    def __init__(self, stdout_reader, stop_event):
        self.stdout = stdout_reader
        self._stop = stop_event
        self._returncode = None
        self.pid = -1

    @property
    def returncode(self):
        return self._returncode

    @returncode.setter
    def returncode(self, v):
        self._returncode = v

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        deadline = time.time() + timeout if timeout else None
        while self._returncode is None:
            if deadline and time.time() >= deadline:
                break
            time.sleep(0.05)
        return self._returncode if self._returncode is not None else -1

    def terminate(self):
        self._stop.set()
        self._returncode = -15


class _QueueLineReader:
    """queue.Queue[str] → 类文件对象（readline）。"""

    def __init__(self, q: queue.Queue, stop: threading.Event):
        self._q = q
        self._stop = stop
        self._buf = ""

    def readline(self) -> str:
        while True:
            if "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                return line + "\n"
            try:
                chunk = self._q.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    if self._buf:
                        remaining = self._buf
                        self._buf = ""
                        return remaining
                    return ""
                continue
            if chunk is None:
                if self._buf:
                    remaining = self._buf
                    self._buf = ""
                    return remaining
                return ""
            self._buf += chunk

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line


def _locate_cli_session_jsonl(session_id: str, cwd: str | None = None) -> str | None:
    """在 CLI 会话目录中查找 session jsonl 文件。

    搜索路径：
    1. AGENT_OS_CLI_HOME 环境变量
    2. ~/.codebuddy/projects/
    3. ~/.claude/projects/
    """
    try:
        from ..utils import cwd_to_session_key
    except ImportError:
        try:
            from src.utils import cwd_to_session_key
        except ImportError:
            cwd_to_session_key = None

    home = os.path.expanduser("~")
    roots = []

    env_root = os.environ.get("AGENT_OS_CLI_HOME")
    if env_root and os.path.isdir(os.path.join(env_root, "projects")):
        roots.append(env_root)

    for name in [".codebuddy", ".claude"]:
        p = os.path.join(home, name)
        if os.path.isdir(os.path.join(p, "projects")):
            roots.append(p)

    if cwd and cwd_to_session_key:
        key = cwd_to_session_key(cwd)
    else:
        key = None

    filename = f"{session_id}.jsonl"
    for root in roots:
        projects = os.path.join(root, "projects")
        if key:
            path = os.path.join(projects, key, filename)
            if os.path.exists(path):
                return path
        # 扫描所有项目目录
        try:
            for proj in os.listdir(projects):
                candidate = os.path.join(projects, proj, filename)
                if os.path.exists(candidate):
                    return candidate
        except OSError:
            continue

    return None


def _parse_cmd_shim(cli_path: str) -> str | None:
    try:
        with open(cli_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        m = re.search(r'"%_prog%"\s+"(%dp0%[^"]+)"', content)
        if m:
            target_rel = m.group(1)
        else:
            m = re.search(r'@node\s+"(%~dp0[^"]+)"', content)
            if not m:
                return None
            target_rel = m.group(1)
        dp0 = os.path.dirname(cli_path) + os.sep
        target_abs = os.path.normpath(target_rel.replace('%dp0%', dp0).replace('%~dp0', dp0))
        return target_abs if os.path.exists(target_abs) else None
    except Exception:
        return None


def _can_node_load(target: str) -> bool:
    try:
        escaped = target.replace('\\', '\\\\')
        check = subprocess.run(
            ['node', '-e', f'require.resolve("{escaped}")'],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, cwd=os.path.dirname(target),
        )
        return check.returncode == 0
    except Exception:
        return False
