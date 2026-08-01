"""PersistenceMixin — 节流写盘 + EventBus 订阅（从 agent_os.py 拆出）。"""
import logging
import threading

from ..persistence.sqlite import save_runs_to_disk

logger = logging.getLogger("agent_os")


class PersistenceMixin:
    """持久化：节流写盘 + 标脏 + 事件订阅。"""

    def _periodic_save_worker(self):
        """每 3 秒检查一次脏标记，dirty 时写盘（后台线程）。"""
        import time as _time
        while True:
            _time.sleep(3.0)
            try:
                with self._save_lock:
                    if not self._save_dirty:
                        continue
                    self._save_dirty = False
                count = len(self._registry.runs)
                save_runs_to_disk(self)
                logger.info(f"persist: saved {count} runs to disk")
            except Exception as e:
                logger.warning(f"persist worker error: {e}")

    def _mark_dirty(self):
        """标记需要持久化（线程安全）。"""
        self._save_dirty = True

    def _on_run_event(self, payload: dict) -> None:
        """EventBus 订阅者：收到 run.event 时唤醒对应 run 的 SSE。"""
        run_id = payload.get("run_id")
        ri = self._registry.runs.get(run_id)
        if ri and ri._new_output_event:
            ri._new_output_event.set()
