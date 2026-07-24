"""EventBus — 进程内发布/订阅，线程安全。

解耦 RunInfo 与 SSE 唤醒 / 持久化触发 / 完成信号分发。
RunInfo.add_event 不再直接操作 _loop / _new_output_event / _dirty_callback，
改为 publish 到 bus；StreamOutput / Persistence / Orchestrator 各自订阅关心的 topic。

Topic 约定：
  run.event      — 新结构化事件（payload: run_id, event）
  run.dirty      — 需要持久化（payload: run_id）
  run.completion — agent 完成信号（payload: run_id, exit_code, reported_result, source）
  run.status     — 状态变更，用于唤醒 SSE 重检退出条件（payload: run_id, status）
"""
import asyncio
import inspect
import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("agent_os")


class EventBus:
    """线程安全的发布/订阅总线。

    publish 可在任意线程调用（reader 线程 / HTTP handler / timeout watcher）。
    handler 若为协程函数，调度到 event loop 执行；普通函数在当前线程同步执行
    （handler 自行保证线程安全或保持轻量）。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self._loop = loop
        self._subs: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()

    def subscribe(self, topic: str, handler: Callable) -> None:
        """订阅 topic。handler 签名 (payload: dict) -> None。"""
        with self._lock:
            self._subs.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: Callable) -> None:
        """取消订阅。"""
        with self._lock:
            subs = self._subs.get(topic, [])
            if handler in subs:
                subs.remove(handler)

    def publish(self, topic: str, **payload: Any) -> None:
        """发布事件。可在任意线程调用。"""
        with self._lock:
            handlers = list(self._subs.get(topic, []))
        for handler in handlers:
            self._dispatch(handler, payload)

    def _dispatch(self, handler: Callable, payload: dict) -> None:
        """按 handler 类型分发：协程调度到 loop，普通函数同步执行。"""
        if inspect.iscoroutinefunction(handler):
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    asyncio.ensure_future, handler(payload), self._loop
                )
            else:
                logger.debug(
                    f"EventBus: no running loop, dropping async handler {handler!r}"
                )
        else:
            try:
                handler(payload)
            except Exception as exc:
                logger.warning(f"EventBus handler error ({handler!r}): {exc}")
