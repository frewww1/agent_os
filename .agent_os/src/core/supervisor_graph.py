"""SupervisorGraph — Supervisor 审查循环的 LangGraph 编排。

比 GoalGraph 更复杂：双 agent 循环（被审查者 + 审查者交替），2 个中断点。
- wait_verdict: 中断等 supervisor report.py PASS/CORRECTION
- correct: CORRECTION → 反馈 agent 重做 → 中断等 agent 完成 → resume supervisor

⚠️ 实验性骨架 — 双中断点的时序处理较复杂，当前未集成到 Orchestrator。
Orchestrator 的 supervisor 分支保留原 if/elif 逻辑作为稳定实现。
后续验证时序正确后可切换到此 graph。
"""
import os
import sqlite3
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

import logging

logger = logging.getLogger("agent_os")


class SupervisorState(TypedDict):
    agent_run_id: str        # 被审查的 agent
    supervisor_run_id: str   # 审查 agent
    verdict: Optional[str]   # "PASS" | "CORRECTION" | None
    correction_feedback: str
    review_round: int


class SupervisorGraph:
    """Supervisor 审查循环：创建 supervisor → 等审查 → PASS/CORRECTION。

    2 个中断点：
    1. wait_verdict: 等 supervisor report.py 发 PASS/CORRECTION
    2. correct: 等 agent 重做完成

    resume 需区分"supervisor 完成"（传 verdict）和"agent 完成"（传 None）。
    """

    def __init__(self, pm):
        self._pm = pm
        self._graph = self._build()

    def _spawn_supervisor(self, state: SupervisorState) -> dict:
        """节点：首次创建 supervisor agent。"""
        ri = self._pm.runs.get(state["agent_run_id"])
        if ri is None:
            logger.warning(f"[SupervisorGraph] agent {state['agent_run_id'][:8]} not found")
            return {"verdict": "PASS"}  # 容错：跳过审查
        sup_id = self._pm._orchestrator._spawn_supervisor(ri)
        logger.info(f"[SupervisorGraph] spawned supervisor {sup_id[:8]} for agent {state['agent_run_id'][:8]}")
        return {"supervisor_run_id": sup_id}

    def _wait_verdict(self, state: SupervisorState) -> dict:
        """节点：中断等 supervisor 审查完成（PASS/CORRECTION）。

        外部 resume 时传 "PASS" 或 correction feedback 字符串。
        """
        verdict = interrupt({"waiting_for": state["supervisor_run_id"]})
        if verdict == "PASS":
            logger.info(f"[SupervisorGraph] supervisor PASS for agent {state['agent_run_id'][:8]}")
            return {"verdict": "PASS"}
        logger.info(f"[SupervisorGraph] supervisor CORRECTION for agent {state['agent_run_id'][:8]}")
        return {"verdict": "CORRECTION", "correction_feedback": verdict or "needs improvement"}

    def _correct(self, state: SupervisorState) -> dict:
        """节点：CORRECTION → 反馈 agent 重做 → 中断等 agent 完成 → resume supervisor。"""
        # 反馈 agent
        self._pm.continue_run(
            state["agent_run_id"],
            f"Supervisor correction: {state['correction_feedback']}",
            source="os",
        )
        # 中断等 agent 重做完成
        interrupt({"waiting_for": state["agent_run_id"]})
        # agent 重做完成 → resume supervisor 审查
        ri = self._pm.runs.get(state["agent_run_id"])
        if ri:
            context = self._pm._build_work_context(ri)
            self._pm.continue_run(
                state["supervisor_run_id"],
                f"## Agent 新一轮产出\n\n{context[:8000]}\n\n请继续审查。满意后 report.py --result \"PASS\"",
                source="os",
            )
        return {"review_round": state["review_round"] + 1, "verdict": None}

    def _route(self, state: SupervisorState) -> str:
        """条件路由：PASS → 结束，CORRECTION → correct。"""
        if state.get("verdict") == "PASS":
            return "done"
        return "correct"

    def _build(self):
        g = StateGraph(SupervisorState)
        g.add_node("spawn_sup", self._spawn_supervisor)
        g.add_node("wait_verdict", self._wait_verdict)
        g.add_node("correct", self._correct)
        g.set_entry_point("spawn_sup")
        g.add_edge("spawn_sup", "wait_verdict")
        g.add_conditional_edges("wait_verdict", self._route, {
            "correct": "correct",
            "done": END,
        })
        g.add_edge("correct", "wait_verdict")  # 审查循环
        db_path = os.path.join(self._pm._state_dir, "supervisor_graph.db")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        return g.compile(checkpointer=checkpointer)

    def run(self, agent_run_id: str) -> bool:
        """首次启动 supervisor 审查循环。

        返回 True 如果 supervisor PASS（graph 完成），False 如果暂停等审查。
        """
        logger.info(f"[SupervisorGraph] starting review for agent {agent_run_id[:8]}")
        result = self._graph.invoke({
            "agent_run_id": agent_run_id,
            "supervisor_run_id": "",
            "verdict": None,
            "correction_feedback": "",
            "review_round": 0,
        }, config={"configurable": {"thread_id": agent_run_id}})
        return result.get("verdict") == "PASS"

    def resume_supervisor(self, agent_run_id: str, verdict: str) -> bool:
        """supervisor 审查完成后恢复（verdict = "PASS" 或 correction feedback）。"""
        logger.info(f"[SupervisorGraph] resuming with supervisor verdict for {agent_run_id[:8]}")
        config = {"configurable": {"thread_id": agent_run_id}}
        result = self._graph.invoke(Command(resume=verdict), config=config)
        return result.get("verdict") == "PASS"

    def resume_agent(self, agent_run_id: str) -> bool:
        """agent 重做完成后恢复。"""
        logger.info(f"[SupervisorGraph] resuming after agent redo for {agent_run_id[:8]}")
        config = {"configurable": {"thread_id": agent_run_id}}
        result = self._graph.invoke(Command(resume="agent_redone"), config=config)
        return result.get("verdict") == "PASS"
