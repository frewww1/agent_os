"""AgentBackend 协议 + BaseAgentBackend 基类。"""
import logging
from typing import Iterator, Protocol, runtime_checkable

from ._utils import _locate_cli_session_jsonl
from .cli_resolver import read_models_cache_file, write_models_cache_file

logger = logging.getLogger("agent_os")


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
               ) -> "SessionLike":
        """启动 agent 会话，返回会话句柄。"""
        ...

    def stream(self, handle: "SessionLike") -> Iterator[dict]:
        """流式读取 agent 输出事件。"""
        ...

    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None:
        """返回会话文件路径（jsonl），用于 rewind/clear 操作。"""
        ...


class BaseAgentBackend:
    """Agent 后端基类。

    子类必须实现: launch()
    可选覆写: stream(), get_session_path(), _discover_models()

    模型发现使用模板方法：
      list_models() = read_cache -> _discover_models() -> FALLBACK_MODELS
    """

    FALLBACK_MODELS: list[str] = [
        "claude-sonnet-4.6", "claude-sonnet-4.5", "claude-opus-4.5",
        "deepseek-v4-pro", "deepseek-v4-flash", "gpt-5.1",
    ]

    def list_models(self) -> list[str]:
        """模型发现（模板方法）：缓存 -> 子类发现 -> 兜底。"""
        models = read_models_cache_file()
        if models:
            return models
        models = self._discover_models()
        if models:
            write_models_cache_file(models)
            return models
        return self.FALLBACK_MODELS

    def _discover_models(self) -> list[str]:
        """子类覆写：从特定源发现模型列表。基类返回空。"""
        return []

    def stream(self, handle) -> Iterator[dict]:
        """默认实现：从 subprocess.stdout 逐行读取并解析。

        CLI 后端用此默认实现（handle 是 subprocess.Popen）。
        SDK 后端应覆写为直接 yield 事件（handle 是 SDKHandle）。
        """
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
        """默认实现：CLI 后端的 jsonl 文件路径。

        SDK 后端应覆写此方法（可能没有本地会话文件）。
        """
        return _locate_cli_session_jsonl(session_id, cwd)
