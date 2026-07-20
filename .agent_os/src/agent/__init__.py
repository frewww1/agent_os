"""Agent 底层抽象层 — AgentBackend 协议 + 各后端实现。"""
from .backend import (
    AgentBackend, BaseAgentBackend, SessionHandle,
    get_backend, register_backend,
    NativeBackend, SDKBackend, CodeBuddySDKBackend, OmnigentBackend,
)
