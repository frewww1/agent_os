"""StreamReader — 读取 agent 进程输出 + 解析事件。

从 AgentOS 抽取 _start_reader / _read_output。
通过 __getattr__ 自动转发 self.xxx 到 AgentOS，方法体无需改动。
"""
import logging
import pathlib
import threading
from datetime import datetime

from .models import RunInfo, RunStatus

logger = logging.getLogger("agent_os")


def find_latest_plan_file() -> str | None:
    """返回 ~/.codebuddy/plans/ 下最近修改的 .md 文件路径。"""
    plans_dir = pathlib.Path.home() / ".codebuddy" / "plans"
    if not plans_dir.is_dir():
        return None
    mds = sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(mds[0]) if mds else None


class StreamReader:
    """agent 进程输出读取器。

    启动后台线程读取 backend.stream() 输出，解析事件，
    stream 结束后调 Agent.on_process_exit 定型。
    """

    def __init__(self, pm):
        self._pm = pm

    def __getattr__(self, name):
        """未定义属性自动转发到 AgentOS。"""
        return getattr(self._pm, name)

    def start_reader(self, run_info: RunInfo) -> None:
        """启动后台读取线程。"""
        reader = threading.Thread(
            target=self.read_output,
            args=(run_info,),
            daemon=True,
            name=f"reader-{run_info.run_id[:6]}",
        )
        run_info._reader_thread = reader
        reader.start()

    def read_output(self, run_info: RunInfo) -> None:
        """后台线程：通过 backend.stream() 读取 agent 输出事件。"""
        session = run_info._session
        if session is None:
            logger.error(f"[{run_info.run_id[:8]}] _session is None, cannot read")
            return
        logger.info(f"[{run_info.run_id[:8]}] Reader started, pid={session.pid}")
        try:
            line_count = 0
            for ev in self._backend.stream(session):
                line_count += 1
                if line_count == 1:
                    logger.debug(f"[{run_info.run_id[:8]}] First event processed")

                kind = ev.get("kind", "raw")

                # 特殊事件处理
                if kind == "plan_pending":
                    self._transition(run_info, RunStatus.PLAN_PENDING)
                    plan_file = find_latest_plan_file()
                    if plan_file:
                        run_info.plan_file = plan_file
                        try:
                            with open(plan_file, encoding="utf-8") as f:
                                run_info.plan_content = f.read()
                        except Exception:
                            pass
                    session.terminate()
                    logger.info(f"[{run_info.run_id[:8]}] Plan pending — waiting for user approval")
                elif kind == "system":
                    sid = ev.get("session_id", "")
                    if sid and run_info.session_id and sid != run_info.session_id:
                        run_info.session_id = sid
                elif kind == "result":
                    result_text = ev.get("result", "")
                    if result_text and not run_info.reported_result:
                        run_info._fallback_result = result_text

                # 推结构化事件（自动唤醒 SSE）
                payload = {k: v for k, v in ev.items() if k != "kind"}
                if kind == "plan_pending":
                    payload["run_id"] = run_info.run_id
                run_info.add_event(kind, **payload)

            # stream 结束，等待会话退出
            session.wait()
            run_info.exit_code = session.returncode
            logger.info(f"[{run_info.run_id[:8]}] Session ended: code={session.returncode}, status={run_info.status.value}, events={line_count}")

            # 定型 + 编排逻辑已提取到 _resolve_process_exit（读取与定型分离）
            self._pm.resolve_process_exit(run_info, session.returncode)
            return

        except Exception as e:
            run_info.add_event("error", text=f"[ERROR] {e}")
            self._transition(run_info, RunStatus.FAILED)
            run_info.completed_at = datetime.now()
            self._pm.on_run_completed(run_info)

        finally:
            self._notify_frontend(run_info.run_id)
