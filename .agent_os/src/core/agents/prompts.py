"""统一提示词构建 — 4 类通用块，按 agent 类型组合注入。

1. dag_block      : DAG 工作流（仅 dag 调度根 agent 需要）
2. workspace_block: 共享 workspace（所有 agent 都需要）
3. spawn_block    : 分发子 agent（explore/supervisor/goal 不需要）
4. tools_block    : report.py / send.py（所有 agent；supervisor/goal 定制描述）
"""
import os

# 安装目录（.agent_os）绝对路径。prompts.py 位于 .agent_os/src/core/agents/，
# 需要 dirname × 4 才能到 .agent_os（× 3 会落在 .agent_os/src，脚本路径会
# 多出 src 导致 report.py/send.py/dag.py 找不到）。提示词中的脚本命令一律
# 使用绝对路径，避免 agent 因相对路径解析失败而花时间探索环境。
_AOS_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_AOS_STR = _AOS_DIR.replace("\\", "/")

_WS_DESC = (
    "The workspace is the **shared, persistent context** for this task: "
    "all agents in the same task read and write files here. Write your "
    "outputs as files for other agents to consume, and read files left "
    "by them. Files survive across agent turns and sessions."
)


def workspace_block(workspace_path, header="Shared workspace"):
    """2) workspace —— 所有 agent 都需要。给出绝对路径，不使用 $AGENT_OS_WORKSPACE 占位符。"""
    ws = workspace_path or os.environ.get("AGENT_OS_WORKSPACE", "") or "(unknown)"
    return (
        "## Workspace\n\n"
        f"{header}: {ws}\n"
        "Use this absolute path directly when reading or writing task files "
        "(do NOT rely on any env var).\n\n"
        + _WS_DESC
    )


def dag_block(steps):
    """1) DAG —— 仅 dag 调度根 agent。注入 step 列表与操作指引。"""
    lines = ["## DAG Workflow\n", "Current workflow steps (topological order):"]
    for i, s in enumerate(steps, 1):
        lines.append(f"{i}. {s.get('id')} - {s.get('name', '')} [{s.get('status', 'pending')}]")
    lines.append("")
    lines.append(
        "Drive the workflow with scripts: "
        f"`python {_AOS_STR}/dag.py --ready` to get runnable steps, "
        "spawn each via the Task tool (keep `step_id`), "
        f"`python {_AOS_STR}/dag.py --mark-done <step_id>` after each step, "
        "use `--rerun`/`--add-step` for dynamic changes."
    )
    return "\n".join(lines)


def spawn_block(include=True):
    """3) 分发子 agent —— explore/supervisor/goal 除外。"""
    if not include:
        return ""
    return "- Create sub-agents: use the Task tool to spawn child agents"


def tools_block(task_type):
    """4) report.py / send.py —— 所有 agent；supervisor/goal 定制描述。"""
    if task_type == "supervisor":
        return (
            f"- report.py: submit PASS verdict → "
            f"`python {_AOS_STR}/report.py --result \"<verdict>\"`\n"
            f"- send.py: submit CORRECTION feedback → "
            f"`python {_AOS_STR}/send.py --msg \"<feedback>\"`"
        )
    if task_type == "goal":
        return f"- report.py: reply verdict → `python {_AOS_STR}/report.py --result \"YES/NO\"`"
    return (
        f"- report.py: `python {_AOS_STR}/report.py --result \"<summary>\"`\n"
        f"- send.py: `python {_AOS_STR}/send.py --msg \"<message>\"`"
    )


def compose(task_type, workspace_path, *, task_prompt="", dag_steps=None,
            identity="", extra=""):
    """按 agent 类型组装完整 system prompt。

    - task_type: root / generative / interactive / explore / supervisor / goal
    - dag_steps: 非空且 task_type=root 时注入 DAG 块
    - identity / extra: 覆盖默认身份行 / 追加额外指令（如 supervisor 验证标准）
    """
    ident = identity or {
        "root": "You are running under Agent OS, a multi-agent orchestration system.",
        "generative": "You are a sub-agent under Agent OS.",
        "interactive": "You are an interactive sub-agent under Agent OS.",
        "explore": "You are an explore sub-agent under Agent OS.",
        "supervisor": "你是严格审查 AI agent 工作的监督者。",
        "goal": "You are a concise evaluator. Reply with YES or NO only.",
    }.get(task_type, "You are an Agent OS agent.")

    blocks = [ident]
    if extra:
        blocks.append(extra)

    # 2. workspace —— 所有 agent
    blocks.append(workspace_block(workspace_path))

    # 1. DAG —— 仅 dag 调度根 agent
    if task_type == "root" and dag_steps:
        blocks.append(dag_block(dag_steps))

    # 4. How to Complete（generative/explore 必须 report；interactive 用户 Done）
    if task_type in ("generative", "explore"):
        blocks.append(
            "## How to Complete\n\n"
            f"When done, call `python {_AOS_STR}/report.py --result \"<summary>\"`.\n"
            "- report.py is MANDATORY. Exiting without it = FAILED."
        )
    elif task_type == "interactive":
        blocks.append(
            "## How to Complete\n\n"
            "You are an **interactive** agent. The user will click **Done** to complete you.\n"
            "- Do NOT call report.py — it will be ignored."
        )

    # 3 + 4: Available Tools（spawn + report/send）
    tools = []
    if task_type not in ("explore", "supervisor", "goal"):
        tools.append(spawn_block(True))
    tools.append(tools_block(task_type))
    blocks.append("## Available Tools\n" + "\n".join(tools))

    if task_prompt:
        blocks.append(f"## Task\n{task_prompt}")

    return "\n\n".join(blocks) + "\n"
