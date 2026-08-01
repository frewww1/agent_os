"""SDKBackend — 通用 SDK 后端基类。"""
import json as _json
import logging
import queue
import threading
from typing import Iterator

from .base import BaseAgentBackend
from .session_handle import SDKHandle
from ._utils import _locate_cli_session_jsonl

logger = logging.getLogger("agent_os")


class SDKBackend(BaseAgentBackend):
    """通用 SDK 后端基类。

    子类覆写 _call_sdk() 接入任意 SDK。
    事件直接 yield，不经过 subprocess/JSON 解析。

    会话文件：SDK 底层调 CLI，jsonl 由 CLI 自动维护在 ~/.codebuddy/projects/ 下。
    模型发现：继承 BaseAgentBackend 的模板方法，子类可覆写 _discover_models()。
    """

    def launch(self, prompt: str,
               model: str | None = None,
               session_id: str | None = None,
               resume_session: str | None = None,
               system_prompt: str | None = None,
               cwd: str | None = None,
               env: dict | None = None,
               ) -> SDKHandle:
        handle = SDKHandle(session_id=session_id or "")

        def _run():
            try:
                self._call_sdk(
                    handle=handle,
                    prompt=prompt, model=model or "",
                    session_id=session_id, resume_session=resume_session,
                    system_prompt=system_prompt,
                    cwd=cwd, env=env, stop=handle._stop,
                )
            except Exception as e:
                logger.exception(f"SDK backend error: {e}")
                self._emit_event(handle, "error", error=str(e))
            finally:
                handle._stop.set()
                handle._events_queue.put(None)

        t = threading.Thread(target=_run, daemon=True, name=f"sdk-{session_id or '?'}")
        handle._thread = t
        t.start()
        return handle

    def stream(self, handle: SDKHandle) -> Iterator[dict]:  # noqa: F811
        """SDK 后端：直接从 queue 读取事件 dict，不做 JSON 解析。"""
        q = handle._events_queue
        stop = handle._stop
        if not q or not stop:
            return

        try:
            while True:
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    if stop.is_set():
                        break
                    continue
                if item is None:
                    break
                if isinstance(item, dict):
                    yield item
                elif isinstance(item, str):
                    try:
                        obj = _json.loads(item)
                        if isinstance(obj, dict) and "kind" in obj:
                            yield obj
                        elif isinstance(obj, dict) and "type" in obj:
                            from .stream_parser import parse_stream_json_events
                            for ev in parse_stream_json_events(item):
                                yield ev
                    except Exception:
                        yield {"kind": "raw", "text": str(item)[:500]}
        finally:
            handle.returncode = 0

    def get_session_path(self, session_id: str, cwd: str | None = None) -> str | None:
        return _locate_cli_session_jsonl(session_id, cwd)

    def _emit_event(self, handle: SDKHandle, kind: str, **payload):
        """子类调用此方法推送结构化事件。"""
        handle._events_queue.put({"kind": kind, **payload})

    def _call_sdk(self, handle: SDKHandle, prompt: str, model: str,
                  session_id: str | None, resume_session: str | None,
                  system_prompt: str | None,
                  cwd: str | None, env: dict | None,
                  stop: threading.Event):
        """子类覆写，通过 self._emit_event(handle, kind, ...) 推送事件。"""
        raise NotImplementedError(
            "Subclass must implement _call_sdk(). Use self._emit_event(handle, kind, ...) to push events."
        )
