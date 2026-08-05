"""Agent OS Runtime — 多 Agent 生命周期 + 会话管理 + 编排门面。"""
import asyncio
import json as _json
import logging
import os
import re
import threading
from datetime import datetime
from typing import AsyncGenerator

from .dag import planner as dp
from .env_config import build_agent_env
from .agents import Agent, RootAgent
from .agents.base import RunStatus
from ..persistence.sqlite import load_agents_from_disk, save_agents_to_disk
from ..agent import get_backend

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


class AgentOS:
    """Agent OS Runtime — 多 Agent 进程生命周期管理 + 编排 + 记忆层。

    职责:
    - agents 字典（全局索引）
    - 持久化调度（节流写盘）
    - 超时看护
    - DAG 编排
    - 对外 API（薄委托给 Agent）
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

        self.agents: dict[str, Agent] = {}
        self._models_cache: list[str] | None = None
        self.recorder = None
        self._originally_waiting: set[str] = set()
        self.MAX_GOAL_RETRIES = 5

        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)




        self._state_dir = os.path.join(PROJECT_ROOT, "state")
        os.makedirs(self._state_dir, exist_ok=True)

        load_agents_from_disk(self)
        self._save_task = threading.Thread(
            target=self._periodic_save_worker, daemon=True, name="persist-worker"
        )
        self._save_task.start()

        self._idle_timeout_sec = 20 * 60
        self._timeout_task = self._loop.create_task(self._timeout_watcher())

    # region 持久化

    def _periodic_save_worker(self):
        import time as _time
        while True:
            _time.sleep(3.0)
            try:
                dirty = [a for a in self.agents.values() if a._dirty]
                if not dirty:
                    continue
                for a in dirty:
                    a._dirty = False
                save_agents_to_disk(self)
                logger.info(f"persist: saved {len(self.agents)} agents ({len(dirty)} dirty)")
            except Exception as e:
                logger.warning(f"persist worker error: {e}")

    # ---- 超时看护 ----

    async def _timeout_watcher(self):
        while True:
            await asyncio.sleep(30.0)
            try:
                now = datetime.now()
                for agent in list(self.agents.values()):
                    if agent.status != RunStatus.RUNNING or not agent.idle_timeout_enabled():
                        continue
                    last_ts = self._last_activity_ts(agent)
                    idle_sec = (now - last_ts).total_seconds()
                    if idle_sec > self._idle_timeout_sec:
                        logger.warning(f"[{agent.agent_id[:8]}] idle {idle_sec:.0f}s, force-completing")
                        agent.add_event("error", text=f"[Agent OS] Auto-ended: idle for {int(idle_sec)}s")
                        agent.on_user_done()
            except Exception as e:
                logger.warning(f"timeout watcher error: {e}")

    @staticmethod
    def _last_activity_ts(agent) -> datetime:
        events = list(agent.output_events)
        if events:
            try:
                return datetime.fromisoformat(events[-1].get("ts", agent.started_at.isoformat()))
            except Exception:
                pass
        return agent.started_at

    # ---- 索引 ----

    def _get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def _get_parent(self, parent_id: str | None) -> Agent | None:
        if parent_id and parent_id in self.agents:
            return self.agents[parent_id]
        return None

    def list_models(self, refresh: bool = False) -> list[str]:
        if self._models_cache is not None and not refresh:
            return self._models_cache
        models = self._backend.list_models()
        self._models_cache = models
        return models

    def start_agent(self, prompt: str, agent_name: str | None = None,
                    parent_id: str | None = None,
                    env_extras: dict | None = None,
                    interactive: bool = False,
                    system_prompt: str | None = None,
                    model: str | None = None,
                    task_type: str = "generative",
                    goal: str | None = None,
                    supervisor: str | None = None,
                    workspace_name: str | None = None) -> str:
        import uuid as _uuid
        prompt = Agent._sanitize_unicode(prompt)
        if system_prompt:
            system_prompt = Agent._sanitize_unicode(system_prompt)
        agent_id = _uuid.uuid4().hex[:10]
        session_id = str(_uuid.uuid4())
        if model is None:
            model = self.default_model or "deepseek-v4-pro"
        env = build_agent_env(agent_id, self.project_root, self.port, workspace_name, env_extras)
        if env_extras:
            env.update(env_extras)
        if not parent_id and not system_prompt:
            system_prompt = RootAgent._make_system_prompt(
                env.get("AGENT_OS_WORKSPACE", ".agent_os/workspaces/<agent>/"))

        parent = self._get_parent(parent_id)
        depth = (parent.depth or 0) + 1 if parent else 0

        agent = Agent.for_run(
            backend=self._backend, project_root=self.project_root,
            agent_id=agent_id, prompt=prompt, session_id=session_id,
            parent_id=parent_id, interactive=interactive,
            model=model, task_type=task_type,
            workspace_path=env.get("AGENT_OS_WORKSPACE"),
            step_id=(env_extras or {}).get("AGENT_OS_STEP_ID") if env_extras else None,
            system_prompt=system_prompt, goal=goal, supervisor=supervisor,
        )
        agent.depth = depth
        agent._on_step_done = self._on_agent_step_done
        agent._on_step_start = self._on_agent_step_start
        agent._on_child_created = self._register_child
        if parent:
            agent.parent = parent
            parent.children.append(agent)
        self.agents[agent_id] = agent

        marker_path = os.path.join(self.project_root, ".agent_os_agent_id")
        try:
            with open(marker_path, "w") as f:
                f.write(agent_id)
        except Exception:
            pass

        agent.initialize(prompt, model)
        return agent_id

    def _register_child(self, child):
        """子 agent 创建回调 — 注册到 agents dict。"""
        self.agents[child.agent_id] = child

    # region 薄委托

    def spawn_children(self, parent_id, parent_session_id, tasks, wait_strategy="all"):
        agent = self._get_agent(parent_id)
        if not agent:
            return {"child_count": 0, "child_ids": [], "error": "parent not found"}
        return agent.spawn_children(tasks, parent_session_id, wait_strategy)

    def complete_interactive(self, agent_id: str) -> bool:
        agent = self._get_agent(agent_id)
        if not agent or agent.status != RunStatus.RUNNING:
            return False
        agent.on_user_done()
        return True

    def report_complete(self, agent_id, result):
        agent = self._get_agent(agent_id)
        return agent.on_report(result) if agent else False

    def approve_plan(self, agent_id, feedback="", model=None):
        agent = self._get_agent(agent_id)
        return agent.approve_plan(feedback, model) if agent else False

    def reject_plan(self, agent_id, feedback="", model=None):
        agent = self._get_agent(agent_id)
        return agent.reject_plan(feedback, model) if agent else False

    def continue_agent(self, agent_id, prompt, source="user", model=None, goal=None):
        agent = self._get_agent(agent_id)
        return agent.resume(prompt, source, model, goal) if agent else False

    def stop_agent(self, agent_id):
        agent = self._get_agent(agent_id)
        return agent.stop() if agent else False

    def handle_send(self, agent_id, msg):
        agent = self._get_agent(agent_id)
        return agent.on_send(msg) if agent else False

    def rewind_to(self, agent_id, target_ts):
        agent = self._get_agent(agent_id)
        return agent.rewind_to(target_ts) if agent else {"ok": False, "error": "agent not found"}

    def clear_context(self, agent_id):
        agent = self._get_agent(agent_id)
        return agent.clear_context() if agent else {"ok": False, "error": "agent not found"}

    def set_goal(self, agent_id, goal, max_retries=None):
        agent = self._get_agent(agent_id)
        return agent.set_goal(goal, max_retries) if agent else False

    def skip_goal(self, agent_id):
        agent = self._get_agent(agent_id)
        return agent.skip_goal() if agent else False

    # ---- 管理 & DAG ----

    def delete_agent(self, agent_id: str, recursive: bool = True) -> int:
        agent = self.agents.get(agent_id)
        if not agent:
            return 0
        deleted = 0
        if recursive:
            for child_id in list(agent.children_ids):
                deleted += self.delete_agent(child_id, recursive=True)
        if agent.status == RunStatus.RUNNING:
            agent.terminate()
        if agent.parent_id:
            parent = self.agents.get(agent.parent_id)
            if parent:
                parent.children = [c for c in parent.children if c.agent_id != agent_id]
        try:
            conn = getattr(self, '_db_conn', None)
            if conn:
                conn.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))
                conn.commit()
        except Exception:
            pass
        del self.agents[agent_id]
        return deleted + 1

    def _on_agent_step_done(self, workspace_path: str, step_id: str):
        try:
            dag = dp.load_dag(workspace_path)
            if dp.mark_done(dag.get("steps", []), step_id):
                dp.save_dag(workspace_path, dag)
        except Exception:
            pass

    def _on_agent_step_start(self, workspace_path: str, step_id: str):
        try:
            dag = dp.load_dag(workspace_path)
            if dp.mark_running(dag.get("steps", []), step_id):
                dp.save_dag(workspace_path, dag)
        except Exception:
            pass

    def start_dag(self, workspace_path: str) -> list[str]:
        """从 dag.json 启动所有就绪的 step 作为子 agent。"""
        dag = dp.load_dag(workspace_path)
        ready = dp.ready_steps(dag.get("steps", []))
        if not ready:
            return []
        by_id = {s["id"]: s for s in dag["steps"]}
        ids = []
        for step_id in ready:
            step = by_id[step_id]
            agent_id = self.start_agent(
                prompt=step.get("prompt", ""),
                workspace_name=os.path.basename(workspace_path),
                env_extras={"AGENT_OS_STEP_ID": step_id},
                task_type=step.get("type", "generative"),
                goal=step.get("goal"),
                model=step.get("model"),
            )
            dp.mark_running(dag["steps"], step_id)
            ids.append(agent_id)
        dp.save_dag(workspace_path, dag)
        return ids

    def dag_status(self, agent_id: str) -> dict:
        agent = self.agents.get(agent_id)
        if not agent or not agent.workspace_path:
            return {"ok": False, "error": "agent not found or no workspace"}
        try:
            dag = dp.load_dag(agent.workspace_path)
            steps = dag.get("steps", [])
            order = dp.topo_order(steps) if steps else []
            by_id = {s["id"]: s for s in steps}
            return {"ok": True, "steps": [
                {"id": sid, "name": by_id[sid].get("name", ""),
                 "status": by_id[sid].get("status", "pending"),
                 "depends_on": by_id[sid].get("depends_on", []),
                 "prompt": by_id[sid].get("prompt", "")[:200],
                 "goal": by_id[sid].get("goal"),
                 "max_goal_retries": by_id[sid].get("max_goal_retries", 5),
                 "supervisor": by_id[sid].get("supervisor")}
                for sid in order
            ]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def dag_status_by_workspace(self, workspace_id: str) -> dict:
        """通过 workspace 名称查找 dag 状态（用于 Dashboard 预览）。"""
        for agent in self.agents.values():
            if not agent.workspace_path:
                continue
            ws_name = os.path.basename(agent.workspace_path.rstrip("/\\"))
            if ws_name == workspace_id:
                return self.dag_status(agent.agent_id)
        # 回退：直接从文件系统读
        ws_path = os.path.join(self.project_root, "workspaces", workspace_id)
        if os.path.isdir(ws_path):
            try:
                dag = dp.load_dag(ws_path)
                steps = dag.get("steps", [])
                return {"ok": True, "steps": [
                    {"id": s["id"], "name": s.get("name", ""),
                     "status": s.get("status", "pending"),
                     "depends_on": s.get("depends_on", []),
                     "prompt": s.get("prompt", "")[:200],
                     "goal": s.get("goal"),
                     "supervisor": s.get("supervisor")}
                    for s in steps
                ]}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "workspace not found"}

    def dag_checkout(self, agent_id: str, step_id: str, rerun_downstream: bool = False) -> dict:
        """重置某个 step 状态为 pending，可选重置下游。"""
        agent = self.agents.get(agent_id)
        if not agent or not agent.workspace_path:
            return {"ok": False, "error": "agent not found or no workspace"}
        try:
            dag = dp.load_dag(agent.workspace_path)
            steps = dag.get("steps", [])
            ids_to_reset = [step_id]
            if rerun_downstream:
                descendants = dp.get_descendants(steps, step_id)
                ids_to_reset = descendants
            hit = dp.reset_steps(steps, ids_to_reset)
            if not hit:
                return {"ok": False, "error": f"step {step_id} not found"}
            dp.save_dag(agent.workspace_path, dag)
            return {"ok": True, "step_id": step_id, "reset": hit}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_completed(self) -> int:
        roots = [agent for agent in self.agents.values()
                 if not agent.parent_id and agent.status in (
                     RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED)]
        count = 0
        for a in roots:
            if self.delete_agent(a.agent_id, recursive=True) > 0:
                count += 1
        return count

    # ---- 树查询 ----

    def list_agents(self) -> list[dict]:
        return [agent.to_summary() for agent in self.agents.values()]

    def get_tree(self) -> list[dict]:
        return [agent.to_tree_node() for agent in self.agents.values() if not agent.parent_id]

    @staticmethod
    def unwrap_task_prompt(prompt: str, system_prompt: str = "") -> str:
        if system_prompt:
            m = re.search(r'## Task\n([\s\S]+?)(?=\n## |\Z)', system_prompt)
            if m:
                task = m.group(1).strip()
                if task:
                    return task
        m = re.search(r'\[Your Task\]\n?([\s\S]*?)\n?\[/Your Task\]', prompt)
        if m:
            return m.group(1).strip()
        clean = re.sub(r'\[Agent OS Communication Protocol[\s\S]*?\[/Agent OS Communication Protocol\]\s*', '', prompt)
        clean = re.sub(r'\[Mandatory Closing Step\][\s\S]*?\[/Mandatory Closing Step\]', '', clean).strip()
        return clean.split('\n')[0].strip() or prompt[:80]

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    async def stream_output(self, agent_id: str) -> AsyncGenerator[str, None]:
        agent = self.agents.get(agent_id)
        if not agent:
            return
        loop = asyncio.get_event_loop()
        while True:
            try:
                event = await asyncio.wait_for(
                    loop.run_in_executor(None, agent._event_queue.get), timeout=1.0
                )
                yield _json.dumps(event, ensure_ascii=False)
            except asyncio.TimeoutError:
                if agent.status != RunStatus.RUNNING:
                    break
