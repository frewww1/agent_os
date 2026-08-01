"""环境变量构建 + Task Hook 配置生成 — 纯函数，零 AgentOS 依赖。"""
import logging
import os

from ..utils import sanitize_workspace_name

logger = logging.getLogger("agent_os")


def build_agent_env(run_id: str, project_root: str, port: int,
                    workspace_name: str | None = None,
                    env_extras: dict | None = None) -> dict:
    """构建子进程环境变量（启动 + continue 共用）。"""
    env = os.environ.copy()
    env.update({
        "AGENT_OS_RUN_ID": run_id,
        "AGENT_OS_PORT": str(port),
    })

    # 子 agent 继承父 workspace
    if env_extras and "AGENT_OS_WORKSPACE" in env_extras:
        env["AGENT_OS_WORKSPACE"] = env_extras["AGENT_OS_WORKSPACE"]
        logger.info(f"[{run_id[:8]}] Inherited workspace from parent: {env_extras['AGENT_OS_WORKSPACE']}")
    # 根 agent 新建 workspace
    elif "AGENT_OS_WORKSPACE" not in env:
        dir_name = sanitize_workspace_name(workspace_name) if workspace_name else run_id
        workspace_path = os.path.join(project_root, "workspaces", dir_name)
        reused = os.path.isdir(workspace_path)
        os.makedirs(workspace_path, exist_ok=True)
        env["AGENT_OS_WORKSPACE"] = workspace_path
        action = "Reused" if reused else "Created"
        logger.info(f"[{run_id[:8]}] {action} workspace: {workspace_path}")

    # 透传额外 env（如 AGENT_OS_STEP_ID）
    if env_extras:
        for k, v in env_extras.items():
            if k != "AGENT_OS_WORKSPACE":
                env[k] = v

    return env
