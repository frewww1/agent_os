"""Shared router dependencies — imported by all routers."""
from pathlib import Path

from ...src.utils import safe_run


def get_agent_os():
    """获取全局 AgentOS。由 app.py 在初始化时注入。"""
    from ..app import agent_os
    return agent_os


def get_project_root() -> Path | None:
    agent_os = get_agent_os()
    if agent_os:
        return Path(agent_os.project_root)
    return None
