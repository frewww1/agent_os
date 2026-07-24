"""GoalGraph — Goal 评估循环的 LangGraph 编排。

用 StateGraph 表达 evaluate→feedback→evaluate 循环：
- evaluate: 调 codebuddy 子进程评估 goal 是否达成
- feedback: 反馈评估结果 + interrupt 等 agent 重做完成

集成入口：Agent._goal_step → _run_goal_cycle → GoalGraph.run/resume。
"""
import os
import sqlite3
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

import logging

logger = logging.getLogger("agent_os")


class GoalState(TypedDict):
    run_id: str
    goal: str
    retries: int
    max_retries: int
    is_met: bool
    eval_reason: str


class GoalGraph:
    """Goal 评估循环：评估→未达成→反馈重做→再评估，直到达成或超限。"""

    def __init__(self, pm):
        self._pm = pm
        self._graph = self._build()

    def _evaluate(self, state: GoalState) -> dict:
        """节点：调 codebuddy 子进程评估 goal 是否达成。"""
        ri = self._pm.runs.get(state["run_id"])
        if ri is None:
            logger.warning(f"[GoalGraph] run {state['run_id'][:8]} not found, skipping eval")
            return {"is_met": True, "eval_reason": "run not found, auto-pass"}
        is_met, reason = self._pm._get_agent(ri.run_id)._evaluate_goal()
        logger.info(f"[GoalGraph] eval result: is_met={is_met}, retries={state['retries']}/{state['max_retries']}")
        return {"is_met": is_met, "eval_reason": reason}

    def _feedback(self, state: GoalState) -> dict:
        """节点：反馈评估结果 + interrupt 等 agent 重做完成。"""
        ri = self._pm.runs.get(state["run_id"])
        if ri:
            short = state["eval_reason"].split("\n", 1)[1].strip() if "\n" in state["eval_reason"] else state["eval_reason"]
            ri.add_event(
                "system",
                text=f"[Agent OS] Goal not met ({state['retries'] + 1}/{state['max_retries']}): {short}",
            )
        feedback = (
            f"Your previous attempt did NOT achieve the goal.\n"
            f"Goal: {state['goal']}\n"
            f"Evaluation reason: {state['eval_reason']}\n\n"
            f"Please fix the issues and try again. This is retry "
            f"{state['retries'] + 1}/{state['max_retries']}."
        )
        self._pm.continue_run(state["run_id"], feedback, source="os")
        self._pm._mark_dirty()
        # 中断：等 agent 重做完成。外部调 resume() 恢复。
        interrupt({"waiting_for": state["run_id"], "retry": state["retries"] + 1})
        # resume 后继续（回到 evaluate）
        return {"retries": state["retries"] + 1}

    def _route(self, state: GoalState) -> str:
        """条件路由：达成或超限→结束，否则→反馈。"""
        if state["is_met"]:
            return "done"
        if state["retries"] >= state["max_retries"]:
            return "done"
        return "feedback"

    def _build(self):
        g = StateGraph(GoalState)
        g.add_node("evaluate", self._evaluate)
        g.add_node("feedback", self._feedback)
        g.set_entry_point("evaluate")
        g.add_conditional_edges("evaluate", self._route, {
            "feedback": "feedback",
            "done": END,
        })
        g.add_edge("feedback", "evaluate")
        db_path = os.path.join(self._pm._state_dir, "goal_graph.db")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        return g.compile(checkpointer=checkpointer)

    def run(self, run_id: str, goal: str, max_retries: int = 5) -> bool:
        """首次启动 goal 评估循环。

        返回 True 如果 graph 完成（goal 达成或超限），False 如果暂停等 agent 重做。
        """
        logger.info(f"[GoalGraph] starting goal eval for {run_id[:8]}: {goal[:60]}")
        result = self._graph.invoke({
            "run_id": run_id, "goal": goal,
            "retries": 0, "max_retries": max_retries,
            "is_met": False, "eval_reason": "",
        }, config={"configurable": {"thread_id": run_id}})
        # 如果 graph 到达 END，result 含最终状态；如果 interrupt，result 含暂停状态
        is_met = result.get("is_met", False)
        retries = result.get("retries", 0)
        max_r = result.get("max_retries", max_retries)
        finished = is_met or retries >= max_r
        if finished:
            if is_met:
                logger.info(f"[GoalGraph] goal MET for {run_id[:8]}")
            else:
                logger.info(f"[GoalGraph] goal retries exhausted for {run_id[:8]}")
        return finished

    def resume(self, run_id: str) -> bool:
        """agent 重做完成后恢复 graph。

        返回 True 如果 graph 完成，False 如果再次暂停。
        """
        logger.info(f"[GoalGraph] resuming goal eval for {run_id[:8]}")
        config = {"configurable": {"thread_id": run_id}}
        result = self._graph.invoke(Command(resume="agent_completed"), config=config)
        is_met = result.get("is_met", False)
        retries = result.get("retries", 0)
        max_r = result.get("max_retries", 99)
        finished = is_met or retries >= max_r
        if finished:
            if is_met:
                logger.info(f"[GoalGraph] goal MET for {run_id[:8]} (after retry)")
            else:
                logger.info(f"[GoalGraph] goal retries exhausted for {run_id[:8]} (after retry)")
        return finished
