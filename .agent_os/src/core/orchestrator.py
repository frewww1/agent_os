"""Orchestrator — 流程控制：定型 + 编排决策 + spawn 解析。

从 AgentOS 抽取 _resolve_process_exit（定型）、_on_run_completed（编排）、
resume_parent / _check_spawn_resolution（spawn 解析）。
通过 __getattr__ 自动转发 self.xxx 到 AgentOS。
"""
import asyncio
import logging
import threading
from datetime import datetime

from .models import RunInfo, RunStatus, SpawnRequest
from .prompt_builder import PromptBuilder
from . import dag_planner as dp

logger = logging.getLogger("agent_os")


class Orchestrator:
    """定型 + 编排决策的唯一宿主。

    持有 AgentOS 引用，通过 __getattr__ 自动转发属性访问。
    AgentOS 的 _resolve_process_exit / _on_run_completed 退化为委托。
    """

    def __init__(self, agent_os):
        self._pm = agent_os

    def __getattr__(self, name):
        """未定义属性自动转发到 AgentOS。"""
        return getattr(self._pm, name)

    def resolve_process_exit(self, run_info: RunInfo, exit_code: int | None) -> None:
        """进程退出后的定型 + 编排。按 status 分发到对应 handler。"""
        if run_info.status == RunStatus.PLAN_PENDING:
            self._handle_plan_pending_exit(run_info)
        elif run_info.status == RunStatus.RUNNING:
            self._handle_running_exit(run_info, exit_code)
        elif run_info.status == RunStatus.WAITING:
            self._handle_waiting_exit(run_info, exit_code)
        else:
            logger.debug(f"[{run_info.run_id[:8]}] Status already {run_info.status.value}, skip exit handler")

    def _handle_plan_pending_exit(self, run_info: RunInfo) -> None:
        logger.info(f"[{run_info.run_id[:8]}] Session ended in plan_pending — waiting for user approval")
        self._mark_dirty()

    def _handle_running_exit(self, run_info: RunInfo, exit_code: int | None) -> None:
        if run_info.interactive:
            logger.debug(f"[{run_info.run_id[:8]}] Interactive - staying running")
            return
        if run_info.reported_result:
            self._finalize_reported_run(run_info, exit_code)
        elif run_info.parent_run_id:
            self._finalize_child_run(run_info, exit_code)
        else:
            self._finalize_root_run(run_info, exit_code)

    def _finalize_reported_run(self, run_info: RunInfo, exit_code: int | None) -> None:
        self._transition(run_info, RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        run_info.completed_at = datetime.now()
        final = run_info.reported_result or "(无输出)"
        self._try_record_step_completion(run_info, final)
        # git 功能暂时禁用
        # if run_info.workspace_path:
        #     try:
        #         self.recorder.turn_done(run_info.run_id, len(run_info.turn_markers), run_info.workspace_path)
        #     except Exception:
        #         pass
        logger.info(f"[{run_info.run_id[:8]}] Marked {run_info.status.value}, calling on_run_completed")
        self.on_run_completed(run_info)

    def _finalize_child_run(self, run_info: RunInfo, exit_code: int | None) -> None:
        parent_ri = self.runs.get(run_info.parent_run_id)
        is_active_sup = parent_ri and getattr(parent_ri, '_active_supervisor', None) == run_info.run_id
        sup_done = getattr(run_info, '_supervisor_done', False)
        if is_active_sup or sup_done:
            if sup_done:
                logger.info(f"[{run_info.run_id[:8]}] Supervisor PASS complete")
                self._transition(run_info, RunStatus.COMPLETED)
            else:
                logger.info(f"[{run_info.run_id[:8]}] Supervisor exited without report.py, will be resumed next round")
        else:
            logger.warning(f"[{run_info.run_id[:8]}] Agent exited without calling report.py (code={exit_code}), marking failed")
            run_info.add_event("error", text="[Agent OS] Agent process exited without calling report.py — step failed")
            self._transition(run_info, RunStatus.FAILED)
        run_info.completed_at = datetime.now()
        self.on_run_completed(run_info)

    def _finalize_root_run(self, run_info: RunInfo, exit_code: int | None) -> None:
        logger.info(f"[{run_info.run_id[:8]}] Root agent exited (code={exit_code}), auto-completing")
        self._transition(run_info, RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
        run_info.completed_at = datetime.now()
        logger.info(f"[{run_info.run_id[:8]}] Marked {run_info.status.value}")
        self.on_run_completed(run_info)

    def _handle_waiting_exit(self, run_info: RunInfo, exit_code: int | None) -> None:
        waiting_sup = getattr(run_info, '_waiting_supervisor', None)
        if waiting_sup and waiting_sup in self.runs:
            logger.info(f"[{run_info.run_id[:8]}] Waiting for supervisor review, keep WAITING")
            return
        all_children_done = all(
            not (sr.parent_run_id == run_info.run_id and not sr.is_resolved)
            for sr in self.spawn_requests.values()
        )
        if all_children_done:
            self._transition(run_info, RunStatus.COMPLETED if (exit_code or 0) == 0 else RunStatus.FAILED)
            run_info.completed_at = datetime.now()
            logger.info(f"[{run_info.run_id[:8]}] WAITING agent done, marked {run_info.status.value}")
            final = run_info.reported_result or run_info._fallback_result or "(无输出)"
            self._try_record_step_completion(run_info, final)
        else:
            logger.info(f"[{run_info.run_id[:8]}] WAITING agent ended, children still running")

    MAX_GOAL_RETRIES = 5

    def _spawn_supervisor(self, run_info: RunInfo) -> str:
        """为执行 agent 创建监督 agent，返回 supervisor 的 run_id。

        不阻塞：supervisor 作为子 agent 运行，
        通过 send.py / report.py 发反馈唤醒执行 agent。
        """
        context = PromptBuilder.build_work_context(run_info)
        if not context.strip():
            return ""

        task_desc = run_info.goal or run_info.prompt[:200]
        supervisor_prompt = (
            f"## 审查任务\n{task_desc}\n\n"
            f"## Agent 产出\n{context[:8000]}\n\n"
            f"## 指令\n"
            f"审查 agent 产出是否满足所有标准。\n"
            f"全部满足 → `python .agent_os/report.py --result \"PASS\"` 结束审查\n"
            f"有问题 → `python .agent_os/send.py --msg \"CORRECTION: <具体问题>\"` 告知执行 agent。"
            f"**不要调 report.py**，直接结束即可，下一轮会被自动 resume。"
        )

        sup_system_prompt = (
            f"你是严格审查 AI agent 工作的监督者。\n"
            f"验证 agent 产出是否满足以下所有标准：\n\n"
            f"{run_info.supervisor}\n\n"
            f"Be critical and thorough.\n"
            f"All criteria met → `python .agent_os/report.py --result \"PASS\"`\n"
            f"Issues found → `python .agent_os/send.py --msg \"CORRECTION: <feedback>\"` to the agent.\n"
            f"Do NOT call report.py after sending feedback. Just exit.\n"
            f"You will be resumed automatically for next review round."
        )

        sup_run_id = self.start_run(
            prompt=supervisor_prompt,
            agent_name=f"supervisor-{run_info.run_id[:6]}",
            parent_run_id=run_info.run_id,
            interactive=False,
            system_prompt=sup_system_prompt,
            model=run_info.model,
            task_type="generative",
            env_extras={"AGENT_OS_PARENT_RUN_ID": run_info.run_id},
        )
        logger.info(
            f"[{run_info.run_id[:8]}] Supervisor spawned: {sup_run_id[:8]}, "
            f"waiting for supervisor review"
        )
        return sup_run_id

    def _evaluate_goal(self, run_info: RunInfo) -> tuple[bool, str]:
        """评估 agent 是否达成了 goal。返回 (is_met, reason)。

        起一个独立的 codebuddy 子进程做语义判断。
        上下文从 4 个来源收集后统一截断到 12000 chars。
        """
        goal = run_info.goal or ""
        if not goal:
            return True, "no goal"

        full_context = PromptBuilder.build_work_context(run_info)
        logger.debug(f"[{run_info.run_id[:8]}] _evaluate_goal: context len={len(full_context)}, "
                     f"reported={bool(run_info.reported_result)}, "
                     f"fallback={bool(run_info._fallback_result)}, "
                     f"events={len(run_info.output_events)}, "
                     f"messages={len(run_info.messages)}")
        if not full_context.strip():
            return True, "no content to evaluate (assume met)"

        return self._backend.evaluate(
            goal=goal,
            context=full_context,
            cwd=self.project_root,
        )

    def on_run_completed(self, run_info: RunInfo) -> None:
        """当一个 run 完成时，依次检查 supervisor/graph→goal→spawn。"""
        self._auto_fill_goal(run_info)

        # Supervisor 审查循环
        if self._should_run_supervisor(run_info):
            if not self._run_supervisor_cycle(run_info):
                return
            run_info.supervisor = None

        # Goal 评估循环
        if self._should_run_goal(run_info):
            if not self._run_goal_cycle(run_info):
                return
            run_info.goal = None

        # Spawn 请求匹配
        self._match_spawn_requests(run_info)

    def _auto_fill_goal(self, run_info: RunInfo) -> None:
        """从 dag.json 自动补齐 step goal。"""
        if run_info.goal or not run_info.step_id or not run_info.workspace_path:
            return
        try:
            dag = dp.load_dag(run_info.workspace_path)
            for s in dag.get("steps", []):
                if s.get("id") == run_info.step_id:
                    goal_from_dag = s.get("goal") or ""
                    if goal_from_dag:
                        run_info.goal = goal_from_dag
                        logger.info(f"[{run_info.run_id[:8]}] Goal auto-filled from dag.json for step {run_info.step_id}")
                    break
        except Exception as e:
            logger.debug(f"[{run_info.run_id[:8]}] Goal auto-fill failed: {e}")

    @staticmethod
    def _should_run_supervisor(run_info: RunInfo) -> bool:
        return bool(run_info.supervisor and not run_info.interactive and run_info.status == RunStatus.COMPLETED)

    def _run_supervisor_cycle(self, run_info: RunInfo) -> bool:
        """运行 supervisor graph。返回 True 表示审查完成，False 表示等待中。"""
        if not getattr(run_info, '_supervisor_graph_active', False):
            object.__setattr__(run_info, '_supervisor_graph_active', True)
            run_info.add_event("system", text="[Agent OS] Waiting for supervisor review...")
            return self._pm._supervisor_graph.run(run_info.run_id)
        return self._pm._supervisor_graph.resume_agent(run_info.run_id)

    @staticmethod
    def _should_run_goal(run_info: RunInfo) -> bool:
        return bool(run_info.goal and not run_info.interactive and run_info.status == RunStatus.COMPLETED)

    def _run_goal_cycle(self, run_info: RunInfo) -> bool:
        """运行 goal graph。返回 True 表示评估完成，False 表示等待中。"""
        if not getattr(run_info, '_goal_graph_active', False):
            object.__setattr__(run_info, '_goal_graph_active', True)
            max_retries = getattr(run_info, '_max_goal_retries', None) or self.MAX_GOAL_RETRIES
            return self._pm._goal_graph.run(run_info.run_id, run_info.goal, max_retries)
        return self._pm._goal_graph.resume(run_info.run_id)

    def _match_spawn_requests(self, run_info: RunInfo) -> None:
        """标记完成到关联的 spawn 请求并检查是否可以 resume。"""
        matched_spawn = False
        for spawn_req in self.spawn_requests.values():
            if spawn_req.is_resolved:
                continue
            if run_info.run_id in spawn_req.child_run_ids:
                matched_spawn = True
                spawn_req.completed_children.add(run_info.run_id)
                self.check_spawn_resolution(spawn_req)
        if not matched_spawn:
            logger.debug(
                f"[{run_info.run_id[:8]}] on_run_completed: not found in any active spawn request "
                f"(spawn_requests={len(self.spawn_requests)})"
            )

    def check_spawn_resolution(self, spawn_req: SpawnRequest) -> None:
        """检查 spawn 请求是否满足 resume 条件。"""
        if spawn_req.is_resolved:
            return
        if spawn_req.wait_strategy == "all":
            all_done = all(
                self.runs.get(cid) and self.runs[cid].status in (
                    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED
                )
                for cid in spawn_req.child_run_ids
            )
            should_resume = all_done
        elif spawn_req.wait_strategy == "any":
            should_resume = len(spawn_req.completed_children) > 0
        else:
            should_resume = False

        if should_resume:
            spawn_req.is_resolved = True
            if self._loop and self._loop.is_running():
                asyncio.ensure_future(self._resume_parent_async(spawn_req), loop=self._loop)
            else:
                threading.Thread(
                    target=self.resume_parent, args=(spawn_req,),
                    daemon=True, name=f"resume-{spawn_req.parent_run_id[:6]}"
                ).start()

    async def _resume_parent_async(self, spawn_req: SpawnRequest) -> None:
        """异步包装 resume_parent，在线程池中执行阻塞 I/O。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.resume_parent, spawn_req)

    def resume_parent(self, spawn_req: SpawnRequest) -> None:
        """根据父 agent 进程状态决定 resume 方式。"""
        parent_run_id = spawn_req.parent_run_id
        parent_session_id = spawn_req.parent_session_id
        logger.info(f"resume_parent: parent={parent_run_id[:8]}, "
                    f"spawn_session={parent_session_id[:16] if parent_session_id else 'NONE'}")

        parent = self.runs.get(parent_run_id)
        if not parent:
            logger.error(f"resume_parent: parent {parent_run_id[:8]} not found")
            return

        resume_prompt = self._build_child_results_summary(spawn_req)

        # 父 agent 还在运行 → terminate 后走 continue_run
        parent_alive = parent._session is not None and parent._session.poll() is None
        if parent_alive:
            logger.info(f"resume_parent: parent {parent_run_id[:8]} still running, terminating")
            try:
                parent._session.terminate()
                parent._session.wait(timeout=5)
            except Exception as e:
                logger.warning(f"resume_parent: terminate parent failed: {e}")
            self._transition(parent, RunStatus.COMPLETED)

        session_id = parent.session_id or parent_session_id
        if not session_id:
            logger.error(f"resume_parent: FAILED - no session_id for {parent_run_id[:8]}")
            parent.add_event("error", text="[Agent OS] Error: Cannot resume - no session_id available")
            self._transition(parent, RunStatus.FAILED)
            parent.completed_at = datetime.now()
            return

        parent.session_id = session_id
        logger.info(f"resume_parent: parent {parent_run_id[:8]} calling continue_run")
        ok = self.continue_run(parent_run_id, resume_prompt, source="os")
        if not ok:
            logger.error(f"resume_parent: continue_run returned False for {parent_run_id[:8]}")
            parent.add_event("error", text="[Agent OS] Error: continue_run failed")
            self._transition(parent, RunStatus.FAILED)
            parent.completed_at = datetime.now()

    def _build_child_results_summary(self, spawn_req: SpawnRequest) -> str:
        """组装子 agent 结果摘要文本。"""
        parts = ["子 agent 执行完毕，结果如下：", ""]
        for i, child_id in enumerate(spawn_req.child_run_ids, 1):
            child = self.runs.get(child_id)
            if not child:
                parts.append(f"子任务 {i}：状态未知")
                parts.append("")
                continue

            task_desc = self._registry.unwrap_task_prompt(
                child.prompt, child.system_prompt or "",
            )[:100]
            parts.append(f"子任务 {i}：{task_desc}")

            if child.messages:
                parts.append("过程消息：")
                for m in child.messages:
                    parts.append(f"  - {m['msg']}")

            if child.reported_result:
                status_text = "（用户手动结束）" if child.user_terminated else ""
                parts.append(f"最终结果{status_text}：{child.reported_result}")
            elif child._fallback_result:
                status_text = "（用户手动结束）" if child.user_terminated else ""
                parts.append(f"最终结果{status_text}：{child._fallback_result}")
            elif child.user_terminated:
                parts.append("状态：用户在 Dashboard 手动结束此子任务（无具体输出）")
            else:
                parts.append("最终结果：(未返回结果)")

            parts.append("")

        parts.append("请基于以上结果继续工作。")
        return "\n".join(parts)
