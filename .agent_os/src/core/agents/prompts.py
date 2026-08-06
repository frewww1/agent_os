"""统一提示词构建 — 4 类通用块，按 agent 类型组合注入。

1. dag_block      : DAG 工作流（仅 dag 调度根 agent 需要）
2. workspace_block: 共享 workspace（所有 agent 都需要）
3. spawn_block    : 分发子 agent（explore/supervisor/goal 不需要）
4. tools_block    : report.py / send.py（所有 agent；supervisor/goal 定制描述）
"""
import os

_WS_DESC = (
    "The workspace is the **shared, persistent context** for this task: "
    "all agents in the same task read and write files here. Write your "
    "outputs as files for other agents to consume, and read files left "
    "by them. Files survive across agent turns and sessions."
)


def workspace_block(workspace_path, header="Shared workspace"):
    """2) workspace —— 所有 agent 都需要。"""
    ws = workspace_path or os.environ.get("AGENT_OS_WORKSPACE", "") or "(see $AGENT_OS_WORKSPACE)"
    return (
        "## Workspace\n\n"
        f"{header}: {ws}\n"
        "The env var $AGENT_OS_WORKSPACE points to this absolute path.\n\n"
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
        "`python .agent_os/dag.py --ready` to get runnable steps, "
        "spawn each via the Task tool (keep `step_id`), "
        "`python .agent_os/dag.py --mark-done <step_id>` after each step, "
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
            "- report.py: submit PASS verdict → "
            "`python .agent_os/report.py --result \"<verdict>\"`\n"
            "- send.py: submit CORRECTION feedback → "
            "`python .agent_os/send.py --msg \"<feedback>\"`"
        )
    if task_type == "goal":
        return "- report.py: reply verdict → `python .agent_os/report.py --result \"YES/NO\"`"
    return (
        "- report.py: `python .agent_os/report.py --result \"<summary>\"`\n"
        "- send.py: `python .agent_os/send.py --msg \"<message>\"`"
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
            "When done, call `python .agent_os/report.py --result \"<summary>\"`.\n"
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
