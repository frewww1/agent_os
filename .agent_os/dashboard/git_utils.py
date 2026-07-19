"""Agent OS Dashboard — Git 辅助函数。"""

import re
from pathlib import Path

# 从父模块导入 safe_run（通过 dashboard 的 __init__ 或直接相对导入）
# 这里通过参数注入方式，避免循环依赖


def _get_safe_run():
    """延迟导入 safe_run，避免循环依赖。"""
    from ..src.utils import safe_run
    return safe_run


def get_git_branches(git_dir: Path, task_name: str | None = None) -> list:
    """获取 git 仓库的分支列表，每个分支附带可读名称。
    如果 task_name 不为 None，只返回该任务相关的分支（基准分支 + 衍生分支 -r<N>）。
    返回 [{"name": "sgr_full_...", "display": "task-a", "sha": "a488f1...", "is_base": true}, ...]
    如果 git_dir 不是 git 仓库，返回空列表。"""
    _safe_run = _get_safe_run()
    if not (git_dir / ".git").is_dir():
        return []
    try:
        result = _safe_run(
            ["git", "branch", "--format", "%(refname:short)"],
            cwd=str(git_dir), capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return []
        branch_names = [b.strip() for b in result.stdout.splitlines() if b.strip()]
    except Exception:
        return []

    # 为每个分支获取可读名称
    branches = []
    for name in branch_names:
        # 按任务名过滤：基准分支（精确匹配）或衍生分支（<task_name>-r<数字>）
        if task_name:
            if name != task_name and not is_derived_branch(name, task_name):
                continue

        is_base = (name == task_name) if task_name else False

        info = {"name": name, "display": None, "sha": None, "is_base": is_base}
        try:
            # 获取该分支的 HEAD commit
            r = _safe_run(
                ["git", "rev-parse", "--short=8", name],
                cwd=str(git_dir), capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                info["sha"] = r.stdout.strip()

            # 查找该分支上 agent commit 的 display 名
            r = _safe_run(
                ["git", "log", name, "-F", "--grep=[agent:",
                 "--format=%H%x1f%s", "-n", "50"],
                cwd=str(git_dir), capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                for line in r.stdout.strip().split("\n"):
                    parts = line.split("\x1f", 1)
                    if len(parts) < 2:
                        continue
                    msg = parts[1]
                    # 提取 commit 中的 ws_id：从 "[agent:ws_id:run_id]" 提取 ws_id
                    ws_match = re.match(r'\[agent:([^:]+)', msg)
                    commit_ws_id = ws_match.group(1) if ws_match else ""
                    if not task_name or commit_ws_id == task_name:
                        m = re.match(r'\[agent:[^\]]+\]\s*(.+?)(?:\s*:\s*done)?\s*$', msg)
                        if m:
                            info["display"] = m.group(1).strip()
                        break
            if not info["display"]:
                info["display"] = name
        except Exception:
            pass
        branches.append(info)
    return branches


def is_derived_branch(name: str, task_name: str) -> bool:
    """判断分支名是否为 task_name 的衍生分支（格式: <task_name>-r<数字>）。"""
    return bool(re.match(r'^' + re.escape(task_name) + r'-r\d+$', name))


def get_current_branch(git_dir: Path) -> str:
    """获取当前 git 分支名。如果 git_dir 不是 git 仓库，返回空字符串。"""
    _safe_run = _get_safe_run()
    if not (git_dir / ".git").is_dir():
        return ""
    try:
        result = _safe_run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(git_dir), capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def git_checkout_with_stash(git_base: Path, branch: str, create: bool = False) -> dict:
    """切换 git 分支，自动 stash 未提交更改。
    返回 {"ok": bool, "branch": str, "error": str|None, "warning": str|None}"""
    _safe_run = _get_safe_run()
    wdir = str(git_base)

    def _git(cmd, timeout=10):
        return _safe_run(cmd, cwd=wdir, capture_output=True, text=True, timeout=timeout)

    try:
        # 先尝试直接切换
        if create:
            result = _git(["git", "checkout", "-b", branch])
        else:
            result = _git(["git", "checkout", branch])
        if result.returncode == 0:
            return {"ok": True, "branch": branch}

        # 如果失败，检查是否因为有未提交的更改
        stderr = result.stderr or ""
        if "overwritten by checkout" not in stderr and "commit your changes" not in stderr:
            return {"ok": False, "error": stderr.strip()}

        # 有未提交更改 → stash 后再切换
        stash_result = _git(["git", "stash", "push", "-m", f"auto-stash before switching to {branch}"])
        if stash_result.returncode != 0:
            return {"ok": False, "error": f"stash failed: {stash_result.stderr.strip()}"}

        # 切换分支
        if create:
            co_result = _git(["git", "checkout", "-b", branch])
        else:
            co_result = _git(["git", "checkout", branch])
        if co_result.returncode != 0:
            # 切换失败，恢复 stash
            _git(["git", "stash", "pop"])
            return {"ok": False, "error": f"checkout failed after stash: {co_result.stderr.strip()}"}

        # 恢复 stash（切换成功）
        pop_result = _git(["git", "stash", "pop"])
        if pop_result.returncode != 0:
            return {"ok": True, "branch": branch, "warning": f"stash pop 有冲突: {pop_result.stderr.strip()}"}

        return {"ok": True, "branch": branch}
    except Exception as e:
        return {"ok": False, "error": str(e)}
