"""ReaderMixin — 进程输出读取（从 agents/base.py 拆出）。"""
import logging
import threading
from datetime import datetime

from ..models import RunStatus

logger = logging.getLogger("agent_os")


class ReaderMixin:
    """进程输出读取：后台线程通过 backend.stream() 读取事件。

    stream 结束后直接调 self.on_process_exit 定型。
    """

    def start_reader(self) -> None:
        """启动后台读取线程。"""
        reader = threading.Thread(
            target=self._read_output,
            daemon=True,
            name=f"reader-{self.run_id[:6]}",
        )
        self._ri._reader_thread = reader
        reader.start()

    def _read_output(self) -> None:
        """后台线程：通过 backend.stream() 读取 agent 输出事件。"""
        session = self._session
        if session is None:
            logger.error(f"[{self.run_id[:8]}] _session is None, cannot read")
            return
        logger.info(f"[{self.run_id[:8]}] Reader started, pid={session.pid}")
        try:
            line_count = 0
            for ev in self._backend.stream(session):
                line_count += 1
                if line_count == 1:
                    logger.debug(f"[{self.run_id[:8]}] First event processed")

                kind = ev.get("kind", "raw")

                if kind == "plan_pending":
                    self._transition(RunStatus.PLAN_PENDING)
                    from .base import find_latest_plan_file
                    plan_file = find_latest_plan_file()
                    if plan_file:
                        self._ri.plan_file = plan_file
                        try:
                            with open(plan_file, encoding="utf-8") as f:
                                self._ri.plan_content = f.read()
                        except Exception:
                            pass
                    session.terminate()
                    logger.info(f"[{self.run_id[:8]}] Plan pending — waiting for user approval")
                elif kind == "system":
                    sid = ev.get("session_id", "")
                    if sid and self.session_id and sid != self.session_id:
                        self._ri.session_id = sid
                elif kind == "result":
                    result_text = ev.get("result", "")
                    if result_text and not self.reported_result:
                        self._ri._fallback_result = result_text

                payload = {k: v for k, v in ev.items() if k != "kind"}
                if kind == "plan_pending":
                    payload["run_id"] = self.run_id
                self.add_event(kind, **payload)

            session.wait()
            self._ri.exit_code = session.returncode
            logger.info(f"[{self.run_id[:8]}] Session ended: code={session.returncode}, status={self.status.value}, events={line_count}")

            self.on_process_exit(session.returncode)
            return

        except Exception as e:
            self.add_event("error", text=f"[ERROR] {e}")
            self._transition(RunStatus.FAILED)
            self._ri.completed_at = datetime.now()
            self.on_completed()

        finally:
            self._notify_frontend()
