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

# 提示词语言：AGENT_OS_LANG=zh|en，默认 zh（用户中文环境，避免英文回复）。
# 可通过环境变量配置，compose() 也可按 agent 单独覆盖。
LANG = (os.environ.get("AGENT_OS_LANG") or "zh").strip().lower()
if LANG not in ("zh", "en"):
    LANG = "zh"


def _t(zh: str, en: str) -> str:
    """按当前语言返回文案。"""
    return zh if LANG == "zh" else en


_WS_DESC = _t(
    "工作区是本次任务的**共享、持久上下文**：同一任务的所有 agent 都在这里读写文件。"
    "把你的输出写成文件供其他 agent 消费，也读取它们留下的文件。文件跨 agent 轮次和会话保留。",
    "The workspace is the **shared, persistent context** for this task: "
    "all agents in the same task read and write files here. Write your "
    "outputs as files for other agents to consume, and read files left "
    "by them. Files survive across agent turns and sessions.",
)


def workspace_block(workspace_path, header=None):
    """2) workspace —— 所有 agent 都需要。给出绝对路径，不使用 $AGENT_OS_WORKSPACE 占位符。"""
    ws = workspace_path or os.environ.get("AGENT_OS_WORKSPACE", "") or "(unknown)"
    if header is None:
        header = _t("共享工作区", "Shared workspace")
    return (
        _t("## 工作区", "## Workspace") + "\n\n"
        f"{header}: {ws}\n"
        + _t(
            "读写任务文件时直接使用这个绝对路径（不要依赖任何环境变量）。\n\n",
            "Use this absolute path directly when reading or writing task files "
            "(do NOT rely on any env var).\n\n",
        )
        + _WS_DESC
    )


def dag_block(steps):
    """1) DAG —— 仅 dag 调度根 agent。注入 step 列表与操作指引。"""
    lines = [
        _t("## DAG 工作流", "## DAG Workflow") + "\n",
        _t("当前工作流步骤（拓扑顺序）：", "Current workflow steps (topological order):"),
    ]
    for i, s in enumerate(steps, 1):
        lines.append(f"{i}. {s.get('id')} - {s.get('name', '')} [{s.get('status', 'pending')}]")
    lines.append("")
    lines.append(
        _t("用脚本驱动工作流：", "Drive the workflow with scripts: ")
        + f"`python {_AOS_STR}/dag.py --ready`"
        + _t(" 获取可执行步骤，", " to get runnable steps, ")
        + _t("用 Task 工具分发每个步骤（保留 `step_id`），", "spawn each via the Task tool (keep `step_id`), ")
        + f"`python {_AOS_STR}/dag.py --mark-done <step_id>`"
        + _t(" 每步完成后标记，", " after each step, ")
        + _t("动态调整用 `--rerun`/`--add-step`。", " use `--rerun`/`--add-step` for dynamic changes.")
    )
    return "\n".join(lines)


def spawn_block(include=True):
    """3) 分发子 agent —— explore/supervisor/goal 除外。"""
    if not include:
        return ""
    sub_task_ph = _t("<子任务>", "<sub-task>")
    sub_desc_ph = _t("<子 agent 要做什么>", "<what this sub-agent should do>")
    return (
        _t("- spawn.py：分发子 agent → ", "- spawn.py: distribute sub-agents → ")
        + f"`python {_AOS_STR}/spawn.py --tasks '[{{\"prompt\":\"{sub_task_ph}\"}}]'`\n"
        + _t(
            "  - 把工作拆成相互独立的子任务，每个子任务派一个子 agent；"
            "OS 运行它们，完成后会 resume 你继续。\n",
            "  - Split the work into independent sub-tasks and spawn one agent per sub-task; "
            "the OS runs them and resumes you when they finish.\n",
        )
        + _t(
            f"  - 每个任务项：`{{\"prompt\": \"{sub_desc_ph}\", "
            "\"type\": \"generative|interactive\"}`（model/agent_name 可选）。\n",
            f"  - Each task item: `{{\"prompt\": \"{sub_desc_ph}\", "
            "\"type\": \"generative|interactive\"}` (model/agent_name optional).\n",
        )
        + _t(
            "  - 用 `--poll` 阻塞等待并直接收集所有子结果"
            "（`--poll` 完成后会自动上报汇总结果）。",
            "  - Use `--poll` to block and collect all sub-results directly "
            "(`--poll` auto-reports the aggregated results when done).",
        )
    )


def tools_block(task_type):
    """4) report.py / send.py —— 所有 agent；supervisor/goal 定制描述。"""
    if task_type == "supervisor":
        return (
            _t("- report.py：提交 PASS 结论 → ", "- report.py: submit PASS verdict → ")
            + f"`python {_AOS_STR}/report.py --result \"<verdict>\"`\n"
            + _t(
                "  PASS 语义：**实现已通过全部验收门槛，无需任何修改，任务完成。**"
                " 只有当你亲自验证实现确实满足所有要求时才可 PASS；"
                "审查报告写得规范不代表实现通过，实现仍不合格时必须用 send.py 提交 CORRECTION。\n",
                "  PASS semantics: **the implementation has passed every acceptance gate; "
                "no further changes are needed; the task is COMPLETE.** "
                "Only report PASS when the implementation itself meets all requirements you verified; "
                "a well-written review report does NOT mean the implementation passes — "
                "submit CORRECTION via send.py while the implementation is still failing.\n",
            )
            + _t("- send.py：提交 CORRECTION 反馈 → ", "- send.py: submit CORRECTION feedback → ")
            + f"`python {_AOS_STR}/send.py --msg \"<feedback>\"`"
        )
    if task_type == "goal":
        return (
            _t("- report.py：回复结论 → ", "- report.py: reply verdict → ")
            + f"`python {_AOS_STR}/report.py --result \"YES/NO\"`"
        )
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
        "root": _t(
            "你在 Agent OS（多 agent 编排系统）下运行。",
            "You are running under Agent OS, a multi-agent orchestration system.",
        ),
        "generative": _t("你是 Agent OS 下的一个子 agent。", "You are a sub-agent under Agent OS."),
        "interactive": _t(
            "你是 Agent OS 下的一个交互式子 agent。",
            "You are an interactive sub-agent under Agent OS.",
        ),
        "explore": _t("你是 Agent OS 下的一个探索式子 agent。", "You are an explore sub-agent under Agent OS."),
        "supervisor": _t(
            "你是严格审查 AI agent 工作产出的监督者。你的唯一职责是判定【被监督 agent 的实现】"
            "是否通过全部验收门槛，而不是评判其审查报告写得好不好。"
            "报告 PASS 表示【实现】已完成、无需修改；实现不合格时必须报告 CORRECTION，"
            "即使对方的审查报告格式规范、结论诚实。",
            "You are a strict supervisor reviewing AI agent work output. Your sole duty is to judge "
            "whether the SUPERVISED AGENT'S IMPLEMENTATION meets every acceptance gate, NOT to judge "
            "how well its review report was written. Reporting PASS means the IMPLEMENTATION is complete "
            "and needs no changes; when the implementation is failing you must report CORRECTION "
            "even if its review report is well-formatted and honest.",
        ),
        "goal": _t(
            "你是简洁的评估者，只用 YES 或 NO 回复。",
            "You are a concise evaluator. Reply with YES or NO only.",
        ),
    }.get(task_type, _t("你是 Agent OS 的 agent。", "You are an Agent OS agent."))

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
            _t("## 如何完成", "## How to Complete") + "\n\n"
            + _t("完成后调用 ", "When done, call ")
            + f"`python {_AOS_STR}/report.py --result \"<summary>\"`"
            + _t("。\n- report.py 是必须的，不调用就退出 = 失败。",
                 ".\n- report.py is MANDATORY. Exiting without it = FAILED.")
        )
    elif task_type == "interactive":
        blocks.append(
            _t("## 如何完成", "## How to Complete") + "\n\n"
            + _t(
                "你是**交互式** agent。用户会点击**完成**来结束你。\n"
                "- 不要调用 report.py——会被忽略。",
                "You are an **interactive** agent. The user will click **Done** to complete you.\n"
                "- Do NOT call report.py — it will be ignored.",
            )
        )

    # 3 + 4: Available Tools（spawn + report/send）
    tools = []
    if task_type not in ("explore", "supervisor", "goal"):
        tools.append(spawn_block(True))
    tools.append(tools_block(task_type))
    blocks.append(_t("## 可用工具", "## Available Tools") + "\n" + "\n".join(tools))

    if task_prompt:
        blocks.append(_t("## 任务", "## Task") + f"\n{task_prompt}")

    return "\n\n".join(blocks) + "\n"
