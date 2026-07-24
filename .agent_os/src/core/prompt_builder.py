"""PromptBuilder — 组装 agent 启动参数：system prompt + work context。

纯计算，零 IO，零状态。从 AgentOS 抽取，后续 AgentOS
的方法退化为委托。
"""
import os

from .models import RunInfo


class PromptBuilder:
    """agent 启动参数组装器（纯函数集合）。"""

    @staticmethod
    def build_root_system_prompt(workspace_path: str = "") -> str:
        """根 agent 的 Agent OS system prompt。"""
        ws = workspace_path or ".agent_os/workspaces/<run_id>/"
        return (
            "You are running under Agent OS, a multi-agent orchestration system.\n\n"
            "## Workspace\n\n"
            f"Your workspace is at {ws}\n"
            "This is the persistent file memory for the entire task.\n"
            "The env var $AGENT_OS_WORKSPACE points to this directory.\n"
            "dag.json is at $AGENT_OS_WORKSPACE/dag.json (NOT in the current cwd).\n"
            "Use `python .agent_os/dag.py --ready` which reads from $AGENT_OS_WORKSPACE automatically.\n\n"
            "## Agent Types\n\n"
            "- generative: runs autonomously, calls report.py when done\n"
            "- interactive: waits for user to click Done in the dashboard\n"
            "- explore: cannot spawn children, for exploration tasks\n\n"
            "## Available Tools\n\n"
            "- Create sub-agents: use the Task tool (subagent_type=generative|interactive) "
            "to spawn child agents. Sub-agents share your workspace.\n"
            "- report.py: `python .agent_os/report.py --result \"<summary>\"`\n"
            "  Mark your task as complete. Your parent agent will be resumed.\n"
            "- send.py: `python .agent_os/send.py --msg \"<message>\"`\n"
            "  Send progress updates to your parent agent.\n"
        )

    @staticmethod
    def build_subagent_system_prompt(task_type: str = "generative",
                                     task_prompt: str = "",
                                     workspace_path: str | None = None) -> str:
        """生成注入到子 agent 的 system prompt。"""
        ws_rel = ".agent_os/workspaces/<任务名>/"
        if workspace_path:
            ws_name = os.path.basename(workspace_path.rstrip("/\\"))
            ws_rel = f".agent_os/workspaces/{ws_name}"

        task_prompt = task_prompt.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

        if task_type == "interactive":
            completion = (
                "## How to Complete\n\n"
                "You are an **interactive** agent. Your task requires user input or confirmation.\n"
                "When ready for user review, inform the user what you've done and what input "
                "you need. The user will click **Done** in the dashboard to mark your task as "
                "complete.\n"
                "- ⚠️ Do NOT call report.py — it will be ignored. Only the Done button completes you.\n"
                "- If the user has already provided input, you may still need to wait for Done.\n\n"
            )
        else:
            completion = (
                "## How to Complete\n\n"
                "You are a **generative** agent. You work autonomously and decide when to finish.\n"
                "When your task is complete, you **must** call `python .agent_os/report.py "
                "--result \"<summary>\"` to report your results. Without this, your task will be "
                "marked as **failed** even if the work is done.\n"
                "- ⚠️ report.py is MANDATORY for completion. The process exiting alone is not enough.\n"
                "- The user can also click **Done** to manually complete you at any time.\n\n"
            )

        base = (
            "You are a sub-agent running under Agent OS.\n\n"
            "## Workspace\n\n"
            f"Your shared workspace is at: {ws_rel}\n"
            "This is the persistent file memory for the entire task — "
            "all agents in this pipeline read and write to this same directory. "
            "Files you create here will be accessible to downstream agents.\n\n"
            + completion +
            "## Available Tools\n\n"
            "- Create sub-agents: use the Task tool (subagent_type=generative|interactive) "
            "for further parallel work.\n"
            "- report.py: `python .agent_os/report.py --result \"<summary>\"`\n"
            "  Call this when your task is done. Your parent agent will be resumed.\n"
            "- send.py: `python .agent_os/send.py --msg \"<message>\"`\n"
            "  Send progress updates to your parent agent."
        )
        if task_prompt:
            base += f"\n## Task\n{task_prompt}\n"
        return base

    @staticmethod
    def build_work_context(run_info: RunInfo) -> str:
        """收集 agent 本轮工作产出，构建监督/评估上下文。"""
        parts = []

        if run_info.reported_result:
            parts.append(f"Final report: {run_info.reported_result}")
        elif run_info._fallback_result:
            parts.append(f"Final output: {run_info._fallback_result}")

        # text 事件（完整消息）用换行分隔，text_delta（流式片段）直接拼接
        log_lines = []
        delta_buf = []
        for e in run_info.output_events:
            kind = e.get("kind", "")
            if kind == "text_delta":
                delta_buf.append(e.get("text", ""))
            elif kind in ("text", "tool_result", "report"):
                if delta_buf:
                    log_lines.append("".join(delta_buf))
                    delta_buf = []
                log_lines.append(e.get("text", ""))
        if delta_buf:
            log_lines.append("".join(delta_buf))
        if log_lines:
            parts.append("Work log:\n" + "\n".join(log_lines))

        if run_info.messages:
            msgs = "\n".join(m.get("msg", "") for m in run_info.messages[-15:])
            if msgs.strip():
                parts.append(f"Progress messages: {msgs}")

        return "\n\n".join(parts)[:12000]
