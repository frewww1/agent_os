"""Agent OS Runtime — 多 Agent 生命周期 + 会话管理 + 编排门面。"""
import asyncio
import json as _json
import logging
import os
import threading
from datetime import datetime
from typing import AsyncGenerator

from .dag.service import DagService
from .env_config import build_agent_env
from .infra.event_bus import EventBus
from .registry import Registry
from .infra.run_state_machine import RunStateMachine
from .graph.goal import GoalGraph
from .graph.supervisor import SupervisorGraph
from .agents import Agent
from .models import RunStatus, RunInfo
from ..persistence.sqlite import load_runs_from_disk
from ..agent import get_backend
from ._launch import LaunchMixin
from ._persistence import PersistenceMixin
from ._watcher import WatcherMixin

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "agent_os.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("agent_os")


class AgentOS(LaunchMixin, PersistenceMixin, WatcherMixin):
    """Agent OS Runtime — 多 Agent 进程生命周期管理 + 编排 + 记忆层。

    Mixin 组合:
    - LaunchMixin: 进程启动 3 阶段管道 + resume
    - PersistenceMixin: 节流写盘 + EventBus 订阅
    - WatcherMixin: 超时看护 + 重启恢复
    """

    def __init__(self, project_root: str = ".", cli_command: str = "claude", port: int = 8420,
                 default_model: str | None = None, loop: asyncio.AbstractEventLoop | None = None,
                 backend_type: str | None = None):
        self.project_root = project_root
        self.port = port
        self.default_model = default_model
        _bt = backend_type or os.environ.get("AGENT_OS_BACKEND", "native")
        self._backend = get_backend(_bt, cli_command=cli_command)
        self.cli_command = cli_command
        logger.info(f"AgentOS: backend={type(self._backend).__name__}")
        self._registry = Registry()
        self._models_cache: list[str] | None = None
        self.recorder = None

        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)

        self._bus = EventBus(self._loop)
        self._bus.subscribe("run.dirty", lambda _payload: self._mark_dirty())
        self._bus.subscribe("run.event", self._on_run_event)
        self._dag_service = DagService(None, self.project_root, self._registry.runs)

        self._state_dir = os.path.join(PROJECT_ROOT, "state")
        os.makedirs(self._state_dir, exist_ok=True)
        self._runs_file = os.path.join(self._state_dir, "runs.json")

        self._goal_graph = GoalGraph(self)
        self._supervisor_graph = SupervisorGraph(self)

        self._agents: dict[str, Agent] = {}
        load_runs_from_disk(self)
        self._sync_agents()
        self._save_lock = threading.Lock()
        self._save_dirty = False
        self._save_task = threading.Thread(
            target=self._periodic_save_worker, daemon=True, name="persist-worker"
        )
        self._save_task.start()

        self._idle_timeout_sec = 20 * 60
        self._timeout_task = self._loop.create_task(self._timeout_watcher())

    @property
    def runs(self) -> dict:
        return self._registry.runs

    @property
    def spawn_requests(self) -> dict:
        return self._registry.spawn_requests

    def _sync_agents(self) -> None:
        for run_id, ri in self._registry.runs.items():
            if run_id not in self._agents:
                self._agents[run_id] = Agent.for_run(ri, self)

    def _get_agent(self, run_id: str) -> Agent | None:
        ri = self._registry.runs.get(run_id)
        if not ri:
            return None
        agent = self._agents.get(run_id)
        if not agent:
            agent = Agent.for_run(ri, self)
            self._agents[run_id] = agent
        return agent

    def _start_reader(self, run_id: str) -> None:
        agent = self._get_agent(run_id)
        if agent:
            agent.start_reader()

    def on_run_completed(self, run_info: RunInfo) -> None:
        agent = self._get_agent(run_info.run_id)
        if agent:
            agent.on_completed()

    @staticmethod
    def _sanitize_unicode(text: str) -> str:
        return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    def _get_parent(self, parent_run_id: str | None):
        if parent_run_id and parent_run_id in self._registry.runs:
            return self._registry.runs[parent_run_id]
        return None

    def _notify_frontend(self, run_id: str) -> None:
        self._bus.publish("run.event", run_id=run_id)

    def _notify_and_save(self, run_id: str) -> None:
        self._bus.publish("run.event", run_id=run_id)
        self._mark_dirty()

    def _transition(self, run_info: RunInfo, to_status: RunStatus) -> None:
        from_status = run_info.status
        if from_status == to_status:
            return
        if not RunStateMachine.can_transition(from_status.value, to_status.value):
            logger.warning(
                f"[{run_info.run_id[:8]}] transition {from_status.value} -> {to_status.value} "
                f"not in valid set (allowing for compatibility)"
            )
        run_info.status = to_status
        if to_status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED):
            run_info.completed_at = datetime.now()
        self._mark_dirty()

    def list_models(self, refresh: bool = False) -> list[str]:
        if self._models_cache is not None and not refresh:
            return self._models_cache
        models = self._backend.list_models()
        self._models_cache = models
        return models

    def start_run(self, prompt: str, agent_name: str | None = None,
                  parent_run_id: str | None = None,
                  env_extras: dict | None = None,
                  interactive: bool = False,
                  system_prompt: str | None = None,
                  model: str | None = None,
                  task_type: str = "generative",
                  goal: str | None = None,
                  supervisor: str | None = None,
                  workspace_name: str | None = None) -> str:
        config = self._prepare_launch(prompt, parent_run_id, interactive, env_extras,
                                       system_prompt, model, workspace_name, goal)
        handle = self._launch_process(config)
        if isinstance(handle, dict):
            return handle
        return self._finalize_start(config, handle, parent_run_id, task_type, goal, supervisor)

    # ---- 薄委托 ----

    def spawn_children(self, parent_run_id, parent_session_id, tasks, wait_strategy="all"):
        agent = self._get_agent(parent_run_id)
        if not agent:
            return {"spawn_id": "", "child_count": 0, "child_run_ids": [], "error": "parent not found"}
        return agent.spawn_children(tasks, parent_session_id, wait_strategy)

    def complete_interactive(self, run_id: str) -> bool:
        ri = self._registry.runs.get(run_id)
        if not ri or ri.status != RunStatus.RUNNING:
            return False
        agent = self._get_agent(run_id)
        if not agent:
            return False
        agent.on_user_done()
        return True

    def report_complete(self, run_id, result):
        agent = self._get_agent(run_id)
        return agent.on_report(result) if agent else False

    def approve_plan(self, run_id, feedback="", model=None):
        agent = self._get_agent(run_id)
        return agent.approve_plan(feedback, model) if agent else False

    def reject_plan(self, run_id, feedback="", model=None):
        agent = self._get_agent(run_id)
        return agent.reject_plan(feedback, model) if agent else False

    def continue_run(self, run_id, prompt, source="user", model=None, goal=None):
        agent = self._get_agent(run_id)
        return agent.resume(prompt, source, model, goal) if agent else False

    def stop_run(self, run_id):
        agent = self._get_agent(run_id)
        return agent.stop() if agent else False

    def handle_send(self, run_id, msg):
        agent = self._get_agent(run_id)
        return agent.on_send(msg) if agent else False

    def rewind_to(self, run_id, target_seq):
        agent = self._get_agent(run_id)
        return agent.rewind_to(target_seq) if agent else {"ok": False, "error": "run not found"}

    def clear_context(self, run_id):
        agent = self._get_agent(run_id)
        return agent.clear_context() if agent else {"ok": False, "error": "run not found"}

    def set_goal(self, run_id, goal, max_retries=None):
        agent = self._get_agent(run_id)
        return agent.set_goal(goal, max_retries) if agent else False

    def skip_goal(self, run_id):
        agent = self._get_agent(run_id)
        return agent.skip_goal() if agent else False

    # ---- 管理 & DAG ----

    def delete_run(self, run_id: str, recursive: bool = True) -> int:
        run_info = self._registry.runs.get(run_id)
        if not run_info:
            return 0
        deleted = 0
        if recursive:
            for child_id in list(run_info.children_run_ids):
                deleted += self.delete_run(child_id, recursive=True)
        if run_info.status == RunStatus.RUNNING and run_info._session:
            try:
                run_info._session.terminate()
            except Exception:
                pass
        if run_info.parent_run_id:
            parent = self._registry.runs.get(run_info.parent_run_id)
            if parent and run_id in parent.children_run_ids:
                parent.children_run_ids.remove(run_id)
        for sr in list(self._registry.spawn_requests.values()):
            if run_id in sr.child_run_ids:
                try:
                    sr.child_run_ids.remove(run_id)
                except ValueError:
                    pass
            sr.completed_children.discard(run_id)
        try:
            conn = getattr(self, '_db_conn', None)
            if conn:
                conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
                conn.commit()
        except Exception:
            pass
        del self._registry.runs[run_id]
        self._agents.pop(run_id, None)
        return deleted + 1

    def dag_checkout(self, run_id, step_id, rerun_downstream=False):
        result = self._dag_service.dag_checkout(run_id, step_id, rerun_downstream)
        if not result.get("ok"):
            return result
        ws = self._dag_service._resolve_workspace(run_id)
        if ws:
            to_remove = [rid for rid, ri in self._registry.runs.items()
                         if ri.workspace_path == ws and ri.parent_run_id is not None]
            for rid in to_remove:
                self.delete_run(rid, recursive=False)
        self._mark_dirty()
        return result

    def dag_status_by_workspace(self, workspace_id):
        return self._dag_service.dag_status_by_workspace(workspace_id)

    def dag_status(self, run_id):
        return self._dag_service.dag_status(run_id)

    def clear_completed(self) -> int:
        roots = [ri for ri in self._registry.runs.values()
                 if not ri.parent_run_id and ri.status in (
                     RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED)]
        count = 0
        for r in roots:
            if self.delete_run(r.run_id, recursive=True) > 0:
                count += 1
        return count

    def list_runs(self):
        return self._registry.list_runs()

    def get_tree(self):
        return self._registry.get_tree()

    def get_run(self, run_id: str) -> RunInfo | None:
        return self._registry.get(run_id)

    async def stream_output(self, run_id: str) -> AsyncGenerator[str, None]:
        run_info = self._registry.runs.get(run_id)
        if not run_info:
            return
        cursor = 0
        while True:
            events = list(run_info.output_events)
            while cursor < len(events):
                yield _json.dumps(events[cursor], ensure_ascii=False)
                cursor += 1
            if run_info.status != RunStatus.RUNNING:
                break
            run_info._new_output_event.clear()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, run_info._new_output_event.wait, 1.0
                )
            except asyncio.TimeoutError:
                continue
