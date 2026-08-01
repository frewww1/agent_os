"""WatcherMixin — 超时看护 + 重启恢复（从 agent_os.py 拆出）。"""
import asyncio
import logging
import threading
from datetime import datetime

from .models import RunStatus, RunInfo

logger = logging.getLogger("agent_os")


class WatcherMixin:
    """超时看护 + 重启后父 agent 恢复。"""

    async def _timeout_watcher(self):
        """每 30s 扫一次所有 RUNNING 的子 agent，超过 idle_timeout 强制结束。"""
        while True:
            await asyncio.sleep(30.0)
            try:
                now = datetime.now()
                victims = []
                for ri in list(self._registry.runs.values()):
                    if ri.status != RunStatus.RUNNING:
                        continue
                    agent = self._get_agent(ri.run_id)
                    if agent and not agent.idle_timeout_enabled():
                        continue
                    last_ts = self._last_activity_ts(ri)
                    idle_sec = (now - last_ts).total_seconds()
                    if idle_sec > self._idle_timeout_sec:
                        victims.append((ri.run_id, idle_sec))
                for run_id, idle_sec in victims:
                    logger.warning(f"[{run_id[:8]}] idle {idle_sec:.0f}s > {self._idle_timeout_sec}s, force-completing")
                    agent = self._get_agent(run_id)
                    if agent:
                        agent.add_event(
                            "error",
                            text=f"[Agent OS] Auto-ended: idle for {int(idle_sec)}s (> {self._idle_timeout_sec}s timeout)",
                        )
                        agent.on_user_done()
            except Exception as e:
                logger.warning(f"timeout watcher error: {e}")

    @staticmethod
    def _last_activity_ts(ri: RunInfo) -> datetime:
        """取最后一个事件的时间戳，没有就用 started_at。"""
        events = list(ri.output_events)
        if events:
            try:
                return datetime.fromisoformat(events[-1].get("ts", ri.started_at.isoformat()))
            except Exception:
                pass
        return ri.started_at

    def _resume_restored_parents(self):
        """重启后恢复已完成的父 agent。"""
        resumed_count = 0
        for spawn_id, spawn_req in list(self._registry.spawn_requests.items()):
            if not spawn_req.is_resolved:
                continue
            parent_id = spawn_req.parent_run_id
            parent = self._registry.runs.get(parent_id)
            if not parent or not parent.session_id:
                continue
            if parent.status != RunStatus.STOPPED:
                continue
            logger.info(f"_resume_restored_parents: resuming {parent_id[:8]} ({len(spawn_req.child_run_ids)} children)")
            parent_agent = self._get_agent(parent_id)
            if not parent_agent:
                continue
            if self._loop and self._loop.is_running():
                asyncio.ensure_future(parent_agent._on_children_resolved_async(spawn_req), loop=self._loop)
            else:
                threading.Thread(
                    target=parent_agent._on_children_resolved, args=(spawn_req,),
                    daemon=True, name=f"restore-resume-{parent_id[:6]}"
                ).start()
            resumed_count += 1
        if resumed_count:
            logger.info(f"_resume_restored_parents: resumed {resumed_count} parent(s)")
