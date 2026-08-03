"""Agent 底层适配层 — 后端 + 流解析 + CLI 工具。"""
from .backend import (
    AgentBackend, BaseAgentBackend,
    NativeBackend, SDKBackend,
    SessionLike, SDKHandle,
    get_backend, register_backend,
)
from .codebuddy_sdk import CodeBuddySDKBackend
from . import stream_parser
from . import cli_resolver

__all__ = [
    "AgentBackend", "BaseAgentBackend",
    "NativeBackend", "SDKBackend", "CodeBuddySDKBackend",
    "SessionLike", "SDKHandle",
    "get_backend", "register_backend",
    "stream_parser", "cli_resolver",
]
