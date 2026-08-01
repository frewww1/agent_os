"""会话句柄 — 统一 CLI 进程和 SDK 线程的生命周期接口。

CLI 模式：launch() 返回 subprocess.Popen（自带 poll/wait/terminate/returncode/pid）。
SDK 模式：launch() 返回 SDKHandle（封装 queue+thread+stop_event）。

两者都满足 SessionLike 协议（poll/wait/terminate/returncode/pid）。
"""
import queue
import threading
from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionLike(Protocol):
    """会话句柄协议 — CLI 的 Popen 和 SDK 的 SDKHandle 都满足。"""

    @property
    def returncode(self) -> int | None: ...
    @property
    def pid(self) -> int: ...
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...


class SDKHandle:
    """SDK 模式的会话句柄 — 封装 queue + thread + stop_event。

    只暴露生命周期方法（poll/wait/terminate），queue 通过 _events_queue 属性访问。
    """

    def __init__(self, session_id: str = ""):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._returncode: int | None = None
        self._events_queue: queue.Queue = queue.Queue()
        self.session_id = session_id
        self.pid = -1

    @property
    def returncode(self) -> int | None:
        return self._returncode

    @returncode.setter
    def returncode(self, value: int | None):
        self._returncode = value

    def poll(self) -> int | None:
        if self._thread and not self._thread.is_alive():
            return self._returncode or 0
        return None

    def wait(self, timeout: float | None = None) -> int:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return self._returncode if self._returncode is not None else -1

    def terminate(self) -> None:
        self._stop.set()
        self._returncode = -15

