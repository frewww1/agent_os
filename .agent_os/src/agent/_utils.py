"""后端内部工具：session jsonl 定位。"""
import logging
import os

logger = logging.getLogger("agent_os")


def _locate_cli_session_jsonl(session_id: str, cwd: str | None = None) -> str | None:
    """在 CLI 会话目录中查找 session jsonl 文件。"""
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
        try:
            for proj in os.listdir(projects):
                candidate = os.path.join(projects, proj, filename)
                if os.path.exists(candidate):
                    return candidate
        except OSError:
            continue

    return None
