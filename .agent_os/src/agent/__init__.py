"""Agent 底层抽象层 — AgentBackend 协议 + 各后端实现 + 流解析。"""
from .session_handle import SessionLike, SDKHandle
from .base import AgentBackend, BaseAgentBackend
from .native import NativeBackend
from .sdk_base import SDKBackend
from .codebuddy_sdk import CodeBuddySDKBackend
from .registry import get_backend, register_backend
from . import cli_resolver
from . import stream_parser

__all__ = [
    "AgentBackend", "BaseAgentBackend",
    "SessionLike", "SDKHandle",
    "get_backend", "register_backend",
    "NativeBackend", "SDKBackend", "CodeBuddySDKBackend",
    "cli_resolver", "stream_parser",
]
