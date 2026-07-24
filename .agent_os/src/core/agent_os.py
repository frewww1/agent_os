"""Agent OS Runtime — 多 Agent 生命周期 + 会话管理 + 编排门面。"""
import asyncio
import json as _json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import AsyncGenerator

from . import dag_planner as dp
from .dag_planner import resolve_task_type
# from ..persistence.git_recorder import Recorder  # TODO: git 功能暂时禁用
from .dag_service import DagService
from .env_config import build_agent_env
from .session_manager import SessionManager
from .event_bus import EventBus
from .registry import Registry
from .prompt_builder import PromptBuilder
from .orchestrator import Orchestrator
from .run_state_machine import RunStateMachine
from .goal_graph import GoalGraph
from .supervisor_graph import SupervisorGraph
from .stream_reader import StreamReader
from .models import RunStatus, RunInfo, SpawnRequest
from ..persistence.sqlite import save_runs_to_disk, load_runs_from_disk
from ..agent.backend import get_backend

USE_SHELL = False  # 不用 shell=True，避免特殊字符被 CMD 解析破坏参数

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 日志配置
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

def find_latest_plan_file() -> str | None:
    """返回 ~/.codebuddy/plans/ 下最近修改的 .md 文件路径。"""
    import pathlib
    plans_dir = pathlib.Path.home() / ".codebuddy" / "plans"
    if not plans_dir.is_dir():
        return None
    mds = sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(mds[0]) if mds else None

class AgentOS:
    """Agent OS Runtime — 多 Agent 进程生命周期管理 + 编排 + 记忆层。"""


    # region __init__
    def __init__(self, project_root: str = ".", cli_command: str = "claude", port: int = 8420,
                 default_model: str | None = None, loop: asyncio.AbstractEventLoop | None = None,
                 backend_type: str | None = None):
        self.project_root = project_root
        self.port = port
        self.default_model = default_model
        # Agent 后端：通过环境变量或参数选择
        _bt = backend_type or os.environ.get("AGENT_OS_BACKEND", "native")
        self._backend = get_backend(_bt, cli_command=cli_command)
        self.cli_command = cli_command  # 保留用于日志和 fallback
        logger.info(f"AgentOS: backend={type(self._backend).__name__}")
        self._registry = Registry()
        self._resume_callback = None  # set by main.py for async resume
        self._models_cache: list[str] | None = None  # `<cli> --help` 解析出的模型列表缓存
        # self.recorder = Recorder(project_root=self.project_root)  # TODO: git 功能暂时禁用
        self.recorder = None

        # asyncio 事件循环引用（供后台任务使用）
        # Python 3.10+ 兼容：get_event_loop() 无 running loop 时抛 RuntimeError
        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)

        # EventBus：解耦 RunInfo 与 SSE 唤醒 / 持久化触发
        self._bus = EventBus(self._loop)
        self._bus.subscribe("run.dirty", lambda _payload: self._mark_dirty())
        self._bus.subscribe("run.event", self._on_run_event)
        self._orchestrator = Orchestrator(self)
        self._dag_service = DagService(None, self.project_root, self._registry.runs)  # recorder disabled
        self._session_manager = SessionManager(self._backend)
        self._goal_graph = GoalGraph(self)
        self._supervisor_graph = SupervisorGraph(self)
        self._stream_reader = StreamReader(self)

        # 持久化文件
        self._state_dir = os.path.join(PROJECT_ROOT, "state")
        os.makedirs(self._state_dir, exist_ok=True)
        self._runs_file = os.path.join(self._state_dir, "runs.json")
        # 启动时尝试恢复历史 runs（仅元数据 + 事件流；进程引用全部丢失，
        # 这些 run 之后都是只读的，能查看 / 删除 / 导出但不能 resume）
        load_runs_from_disk(self)
        # 节流写盘：每次 add_event 不直接落盘，由后台线程定期落盘
        self._save_lock = threading.Lock()
        self._save_dirty = False
        self._save_task = threading.Thread(
            target=self._periodic_save_worker, daemon=True, name="persist-worker"
        )
        self._save_task.start()

        # 子 agent 超时看护：默认 20 分钟无新事件视为 stale
        self._idle_timeout_sec = 20 * 60
        self._timeout_task = self._loop.create_task(self._timeout_watcher())

    # endregion

    @property
    def runs(self) -> dict:
        """转发到 Registry。"""
        return self._registry.runs

    @property
    def spawn_requests(self) -> dict:
        """转发到 Registry。"""
        return self._registry.spawn_requests

    # region 工具 & 持久化
    @staticmethod
    def _sanitize_unicode(text: str) -> str:
        """清除 surrogate 字符，防止污染内存和序列化。"""
        return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    def _get_parent(self, parent_run_id: str | None):
        """安全获取 parent RunInfo，不存在返回 None。"""
        if parent_run_id and parent_run_id in self._registry.runs:
            return self._registry.runs[parent_run_id]
        return None

    def _notify_frontend(self, run_id: str) -> None:
        """通过 EventBus 唤醒 SSE 前端。"""
        self._bus.publish("run.event", run_id=run_id)

    def _notify_and_save(self, run_id: str) -> None:
        """唤醒 SSE 前端 + 标记持久化脏数据。"""
        self._bus.publish("run.event", run_id=run_id)
        self._mark_dirty()

    # ---- 生命周期 ----

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
                    if ri.parent_run_id is None:  # 不超时根 agent（用户在交互）
                        continue
                    if ri.interactive:  # 不超时 interactive agent（等待用户点 Done）
                        continue
                    last_ts = self._last_activity_ts(ri)
                    idle_sec = (now - last_ts).total_seconds()
                    if idle_sec > self._idle_timeout_sec:
                        victims.append((ri.run_id, idle_sec))
                for run_id, idle_sec in victims:
                    logger.warning(f"[{run_id[:8]}] idle {idle_sec:.0f}s > {self._idle_timeout_sec}s, force-completing")
                    ri = self._registry.runs.get(run_id)
                    if ri:
                        ri.add_event(
                            "error",
                            text=f"[Agent OS] Auto-ended: idle for {int(idle_sec)}s (> {self._idle_timeout_sec}s timeout)",
                        )
                    self.complete_interactive(run_id)
            except Exception as e:
                logger.warning(f"timeout watcher error: {e}")

    @staticmethod
    def _last_activity_ts(ri: "RunInfo") -> datetime:
        """取最后一个事件的时间戳，没有就用 started_at。"""
        events = list(ri.output_events)
        if events:
            try:
                return datetime.fromisoformat(events[-1].get("ts", ri.started_at.isoformat()))
            except Exception:
                pass
        return ri.started_at

    # ---------- persistence ----------

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

    # ---- 显式状态转换表 ----

    def _transition(self, run_info: RunInfo, to_status: RunStatus) -> None:
        """唯一状态转换入口。非法转换记录 warning 但不阻断（容错）。

        使用 RunStateMachine（transitions 库）做转换校验。
        终态（COMPLETED/FAILED/STOPPED）自动设 completed_at。
        """
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

    def _resume_restored_parents(self):
        """重启后恢复已完成的父 agent（子 agent 在重启前已通过 Done/report.py 完成）。

        仅恢复 _restore_spawn_requests 标记为已解决的 spawn 请求。
        不解决的情况（子 agent 仅因重启被强制 stopped，无实际结果）由
        用户操作（Done 点击/report.py 调用）触发 _on_run_completed 正常 resume。
        """
        resumed_count = 0
        for spawn_id, spawn_req in list(self._registry.spawn_requests.items()):
            # 只有 genuinely resolved 的 spawn 请求才恢复
            # （_restore_spawn_requests 仅在子 agent 有实际结果时标记 resolved）
            if not spawn_req.is_resolved:
                continue

            parent_id = spawn_req.parent_run_id
            parent = self._registry.runs.get(parent_id)
            if not parent or not parent.session_id:
                continue
            if parent.status != RunStatus.STOPPED:
                continue

            logger.info(
                f"_resume_restored_parents: resuming {parent_id[:8]} "
                f"({len(spawn_req.child_run_ids)} children)"
            )
            if self._loop and self._loop.is_running():
                asyncio.ensure_future(self._resume_parent_async(spawn_req), loop=self._loop)
            else:
                threading.Thread(
                    target=self.resume_parent, args=(spawn_req,),
                    daemon=True, name=f"restore-resume-{parent_id[:6]}"
                ).start()
            resumed_count += 1

        if resumed_count:
            logger.info(
                f"_resume_restored_parents: resumed {resumed_count} parent(s)"
            )

    # endregion


    # region 查询 & 配置
    def list_models(self, refresh: bool = False) -> list[str]:
        """返回当前 CLI 支持的模型 ID 列表，带内存缓存。

        委托给 backend.list_models()（NativeBackend 内部已包含缓存读取 +
        CLI --help 解析逻辑，无需额外回退）。
        """
        if self._models_cache is not None and not refresh:
            return self._models_cache
        models = self._backend.list_models()
        self._models_cache = models
        return models

    def set_resume_callback(self, callback):
        """Set async callback for resuming parent agents."""
        self._resume_callback = callback

    # endregion


    # region Agent 启动
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
        """启动新 agent 会话，返回 run_id。"""
        config = self._prepare_launch(prompt, parent_run_id, interactive, env_extras,
                                       system_prompt, model, workspace_name, goal)
        handle = self._launch_process(config)
        if isinstance(handle, dict):
            return handle  # 启动失败
        return self._finalize_start(config, handle, parent_run_id, task_type, goal, supervisor)

    def _prepare_launch(self, prompt: str, parent_run_id: str | None,
                         interactive: bool, env_extras: dict | None,
                         system_prompt: str | None, model: str | None,
                         workspace_name: str | None, goal: str | None) -> dict:
        """阶段1: sanitize + 生成 ID + 构建 env + system_prompt。"""
        prompt = self._sanitize_unicode(prompt)
        if system_prompt:
            system_prompt = self._sanitize_unicode(system_prompt)
        run_id = uuid.uuid4().hex[:10]
        session_id = str(uuid.uuid4())
        if model is None:
            model = self.default_model
        logger.info(f"start_run: run_id={run_id}, session_id={session_id[:13]}, "
                    f"prompt={prompt[:50]}, interactive={interactive}, model={model}, "
                    f"goal={goal}")
        env = build_agent_env(run_id, self.project_root, self.port, workspace_name, env_extras)
        if env_extras:
            env.update(env_extras)
        if not parent_run_id and not system_prompt:
            system_prompt = PromptBuilder.build_root_system_prompt(
                env.get("AGENT_OS_WORKSPACE", ".agent_os/workspaces/<run>/"))
        return {"run_id": run_id, "session_id": session_id, "prompt": prompt,
                "system_prompt": system_prompt, "model": model, "env": env,
                "interactive": interactive}

    def _launch_process(self, config: dict) -> object:
        """阶段2: 启动子进程。成功返回 handle，失败注册 FAILED run 并返回 dict。"""
        logger.info(f"[{config['run_id'][:8]}] Launching agent...")
        try:
            return self._backend.launch(
                prompt=config["prompt"], model=config["model"],
                session_id=config["session_id"], resume_session=None,
                system_prompt=config["system_prompt"],
                cwd=self.project_root, env=config["env"])
        except Exception as e:
            logger.error(f"[{config['run_id'][:8]}] Launch failed: {e}")
            run_info = RunInfo(run_id=config["run_id"], prompt=config["prompt"],
                               status=RunStatus.FAILED)
            run_info.add_event("error", text=f"[ERROR] Popen failed: {e}")
            self._registry.runs[config["run_id"]] = run_info
            self._mark_dirty()
            return {"error": str(e), "run_id": config["run_id"]}

    def _finalize_start(self, config: dict, handle, parent_run_id: str | None,
                         task_type: str, goal: str | None, supervisor: str | None) -> str:
        """阶段3: 创建 RunInfo → 注册到 registry → 链接 parent → 启动 reader。"""
        run_id = config["run_id"]
        _depth = 0
        parent = self._get_parent(parent_run_id)
        if parent:
            _depth = (getattr(parent, '_depth', 0) or 0) + 1

        run_info = RunInfo(
            run_id=run_id, prompt=config["prompt"],
            session_id=config["session_id"], parent_run_id=parent_run_id,
            interactive=config["interactive"], model=config["model"],
            task_type=task_type,
            workspace_path=config["env"].get("AGENT_OS_WORKSPACE"),
            step_id=config["env"].get("AGENT_OS_STEP_ID"),
            system_prompt=config["system_prompt"],
            goal=goal, supervisor=supervisor,
        )
        object.__setattr__(run_info, '_session', handle)
        object.__setattr__(run_info, '_new_output_event', threading.Event())
        object.__setattr__(run_info, '_bus', self._bus)
        run_info._depth = _depth
        run_info.turn_markers.append((0, config["prompt"]))
        self._registry.runs[run_id] = run_info
        run_info.add_event("turn", index=1)
        run_info.add_event("prompt", text=config["prompt"], role="user", source="user")

        if parent:
            parent.children_run_ids.append(run_id)

        marker_path = os.path.join(self.project_root, ".agent_os_run_id")
        try:
            with open(marker_path, "w") as f:
                f.write(run_id)
        except Exception:
            pass

        self._stream_reader.start_reader(run_info)
        self._mark_dirty()
        return run_id

    # endregion


    # region Agent 孵化
    def spawn_children(self, parent_run_id: str, parent_session_id: str,

                       tasks: list[dict], wait_strategy: str = "all") -> dict:

        """
        批量 spawn 子 agent，返回 spawn 信息。
        每个 task 可指定 type: "generative"(默认) 或 "interactive"
        - generative: agent 自行决定何时调用 report.py 结束；进程退出兜底
        - interactive: 用户在 Dashboard 点 Done 才完成
        所有类型都可以中途调用 send.py 发消息给父 agent。
        每个 task 可指定 model（不指定则继承父 agent 的 model）。
        """
        # 如果没有 parent_session_id，从 runs 中查找
        if not parent_session_id and parent_run_id:
            parent = self._registry.runs.get(parent_run_id)
            logger.debug(f"spawn_children lookup: parent_run_id={parent_run_id}, found={parent is not None}, "
                         f"session_id={parent.session_id if parent else 'N/A'}")
            if parent and parent.session_id:
                parent_session_id = parent.session_id

        # 计算深度并做限制
        depth = 0
        parent = self._get_parent(parent_run_id)
        if parent:
            depth = (getattr(parent, '_depth', 0) or 0) + 1

        # 最多 3 层：根 agent(0) → 子 agent(1) → 孙 agent(2)
        if depth >= 3:
            logger.warning(f"spawn_children: depth={depth} >= 3, rejecting from {parent_run_id[:8]}")
            return {"spawn_id": "", "child_count": 0, "child_run_ids": [],
                    "error": f"max depth 3 exceeded (depth={depth})"}

        # 探索模式 agent 禁止 spawn
        if parent and (parent.interactive or getattr(parent, 'explore_mode', False)):
            if getattr(parent, 'task_type', 'generative') == 'explore':
                logger.warning(f"spawn_children: explore agent {parent_run_id[:8]} cannot spawn")
                return {"spawn_id": "", "child_count": 0, "child_run_ids": [],
                        "error": "explore agent cannot spawn children"}

        # 父 agent 的 model 作为子 agent 默认值
        parent_model = parent.model if parent else None
        parent_workspace = parent.workspace_path if parent else None
        # 取父 agent 的真实 workspace，让子 agent 共享同一目录。
        # 嵌套 spawn 时父的 workspace 是从更上层继承来的，按 run_id 拼路径会断开共享。
        if parent and not parent_workspace:
            parent_workspace = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "workspaces", parent_run_id
            )

        logger.info(f"spawn_children: parent={parent_run_id[:8] if parent_run_id else 'NONE'}, "
                    f"parent_session_id={'YES' if parent_session_id else 'NONE'}, "
                    f"tasks={len(tasks)}, wait={wait_strategy}, parent_model={parent_model}, "
                    f"parent_workspace={parent_workspace}")

        spawn_id = uuid.uuid4().hex[:10]
        child_run_ids = []
        spawned_step_ids = []  # 本次 spawn 命中的 DAG step，循环后统一置 running

        for task in tasks:
            prompt = task.get("prompt", "")
            agent_name = task.get("agent_name")
            task_type = resolve_task_type(task, parent_workspace)
            child_model = task.get("model") or parent_model
            step_id = task.get("step_id")  # DAG step 标识，透传给子 agent env
            goal = task.get("goal")  # DAG step 级 goal（session 级也由此传入）
            supervisor = task.get("supervisor")  # DAG step 级 supervisor
            if isinstance(supervisor, dict):
                supervisor = _json.dumps(supervisor, ensure_ascii=False)
            if not prompt:
                logger.warning(f"spawn_children: task {task.get('id','?')} has no prompt, skipping")
                continue
            if step_id:
                spawned_step_ids.append(step_id)

            # 为子 agent 注入 system prompt：
            # 任务指令合并到 system prompt 中（--append-system-prompt），
            # 确保优先级稳定，不受多轮对话或 CLI 行为影响。
            # 用户 prompt 仅作为最简触发语。
            sub_system_prompt = PromptBuilder.build_subagent_system_prompt(
                task_type, prompt, parent_workspace)

            # 用户 prompt：最简触发语（实际任务指令在 system prompt 中）
            task_hint = prompt.split("\n")[0][:80] if prompt else "task"
            wrapped_prompt = f"[Agent OS] Execute: {task_hint}"

            # 子 agent 继承父 agent 的 workspace
            child_env_extras = {}
            if parent_workspace:
                child_env_extras["AGENT_OS_WORKSPACE"] = parent_workspace
            if step_id:
                child_env_extras["AGENT_OS_STEP_ID"] = step_id

            run_id = self.start_run(
                prompt=wrapped_prompt,
                agent_name=agent_name,
                parent_run_id=parent_run_id,
                interactive=(task_type == "interactive"),
                system_prompt=sub_system_prompt,
                model=child_model,
                task_type=task_type,
                goal=goal,
                supervisor=supervisor,
                env_extras=child_env_extras if child_env_extras else None,
            )
            child_run_ids.append(run_id)

        # DAG 联动：本次 spawn 命中的 step 自动置 running 并写回父 workspace 的
        # dag.json。多个 step 共享同一文件，循环外一次性 load/save。失败不影响
        # spawn 主流程（dag.json 可能不存在，即非 DAG 编排场景）。
        if spawned_step_ids and parent_workspace:
            try:
                dag = dp.load_dag(parent_workspace)
                steps = dag.get("steps", [])
                hit = [sid for sid in spawned_step_ids if dp.mark_running(steps, sid)]
                if hit:
                    dp.save_dag(parent_workspace, dag)
                    logger.info(f"spawn_children: marked running {hit} in {parent_workspace}")
            except Exception as e:
                logger.warning(f"spawn_children: mark_running failed: {e}")

        # 记录 spawn 请求
        spawn_req = SpawnRequest(
            spawn_id=spawn_id,
            parent_run_id=parent_run_id,
            parent_session_id=parent_session_id,
            child_run_ids=child_run_ids,
            wait_strategy=wait_strategy,
        )
        self._registry.spawn_requests[spawn_id] = spawn_req

        # 标记父 agent 为 WAITING
        parent = self._get_parent(parent_run_id)
        if parent:
            self._transition(parent, RunStatus.WAITING)
            parent.spawn_id = spawn_id

        self._mark_dirty()
        return {
            "spawn_id": spawn_id,
            "child_count": len(child_run_ids),
            "child_run_ids": child_run_ids,
        }

    def complete_interactive(self, run_id: str) -> bool:
        """用户点击 Dashboard 'Done' 按钮，强制完成 agent。

        通用实现：对 interactive 和 generative 都生效。
        - 如果进程还在跑：先 terminate 子进程
        - 标记 user_terminated=True，但不主动写 reported_result（用户的"Done"是
          一个信号，不是一次成果汇报；resume 时父 agent 只需要知道"这个子任务
          结束了"，无需注入伪造的最终结果文本）
        - 标记 COMPLETED，触发 resume parent（如果是子 agent）
        """
        run_info = self._registry.runs.get(run_id)
        if not run_info:
            return False
        if run_info.status != RunStatus.RUNNING:
            return False

        # 先停掉子进程（如果还活着）
        proc = run_info._session
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                logger.info(f"[{run_id[:8]}] complete: terminated child process")
            except Exception as e:
                logger.warning(f"[{run_id[:8]}] complete: terminate failed: {e}")

        # 仅打标记 — 不写 reported_result，不动 _fallback_result
        run_info.user_terminated = True
        self._transition(run_info, RunStatus.COMPLETED)
        run_info.completed_at = datetime.now()
        kind_label = "Interactive" if run_info.interactive else "Generative"
        run_info.add_event("system", text=f"[{kind_label} Agent] Ended by user (Done).")
        # 用单独的事件类型，避免和子 agent 自己 report.py 的"final result"混淆
        run_info.add_event("user_done")

        self._try_record_step_completion(run_info, "(用户手动结束)")

        # 唤醒 SSE
        self._notify_frontend(run_info.run_id)

        # 触发 resume 检查
        self._orchestrator.on_run_completed(run_info)
        self._mark_dirty()
        return True

    def report_complete(self, run_id: str, result: str) -> bool:
        """子 agent 调用 report.py 汇报结果。

        generative: 设置 result 并触发 resume。
        interactive: 忽略 — interactive agent 不应调用 report.py，由用户点 Done 结束。
        """
        run_info = self._registry.runs.get(run_id)
        if not run_info:
            return False

        # interactive agent 调用 report.py → 忽略
        if run_info.interactive:
            logger.info(f"[{run_id[:8]}] Interactive agent called report.py — ignored, waiting for user Done")
            return True

        # supervisor agent 调 report.py → resume SupervisorGraph 或唤醒等待中的执行 agent
        if run_info.parent_run_id:
            parent_ri = self._registry.runs.get(run_info.parent_run_id)
            if parent_ri and getattr(parent_ri, '_supervisor_graph_active', False):
                # SupervisorGraph 模式：resume graph with verdict
                result = self._sanitize_unicode(result)
                run_info.reported_result = result
                self._transition(run_info, RunStatus.COMPLETED)
                run_info.completed_at = datetime.now()

                msg_upper = result.strip().upper()
                if msg_upper.startswith("PASS"):
                    parent_ri.add_event("system", text="[Agent OS] Supervisor: PASS — task complete")
                    logger.info(f"[{parent_ri.run_id[:8]}] Supervisor PASS")
                    finished = self._supervisor_graph.resume_supervisor(parent_ri.run_id, "PASS")
                    if not finished:
                        self._mark_dirty()
                        return True
                    parent_ri.supervisor = None
                    self._orchestrator.on_run_completed(parent_ri)
                else:
                    parent_ri.add_event("system", text=f"[Agent OS] Supervisor correction: {result[:200]}")
                    logger.info(f"[{parent_ri.run_id[:8]}] Supervisor CORRECTION, resuming")
                    finished = self._supervisor_graph.resume_supervisor(parent_ri.run_id, result)
                    if not finished:
                        self._mark_dirty()
                        return True

                self._mark_dirty()
                return True
            elif parent_ri and getattr(parent_ri, '_waiting_supervisor', None) == run_id:
                # fallback：原有逻辑（兼容旧数据无 graph）
                result = self._sanitize_unicode(result)
                run_info.reported_result = result
                self._transition(run_info, RunStatus.COMPLETED)
                run_info.completed_at = datetime.now()

                msg_upper = result.strip().upper()
                object.__setattr__(parent_ri, '_waiting_supervisor', None)

                if msg_upper.startswith("PASS"):
                    object.__setattr__(run_info, '_supervisor_done', True)
                    object.__setattr__(parent_ri, '_active_supervisor', None)
                    parent_ri.supervisor = None
                    parent_ri.add_event("system", text="[Agent OS] Supervisor: PASS — task complete")
                    logger.info(f"[{parent_ri.run_id[:8]}] Supervisor PASS")
                    self._orchestrator.on_run_completed(parent_ri)
                elif msg_upper.startswith("CORRECTION"):
                    parent_ri.add_event("system", text=f"[Agent OS] Supervisor correction: {result[:200]}")
                    logger.info(f"[{parent_ri.run_id[:8]}] Supervisor CORRECTION, resuming")
                    self.continue_run(parent_ri.run_id, result, source="os")
                else:
                    parent_ri.add_event("system", text=f"[Agent OS] Supervisor feedback: {result[:200]}")
                    self.continue_run(parent_ri.run_id, result, source="os")

                self._mark_dirty()
                return True

        result = self._sanitize_unicode(result)
        run_info.reported_result = result
        logger.info(f"[{run_id[:8]}] report_complete: status={run_info.status.value}, result={result[:50]}")

        self._try_record_step_completion(run_info, result)

        if run_info.status == RunStatus.RUNNING:
            # 先终止进程，确保后续 _on_run_completed → continue_run 时进程已退出
            proc = run_info._session
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            self._transition(run_info, RunStatus.COMPLETED)
            run_info.completed_at = datetime.now()
            run_info.add_event("report", text=result)
            self._orchestrator.on_run_completed(run_info)
        elif run_info.status == RunStatus.COMPLETED:
            # 进程已经退出但 _on_run_completed 可能已经触发过了
            # 需要再次检查是否所有子任务都完成了（因为这次有了 reported_result）
            self._orchestrator.on_run_completed(run_info)
        else:
            # WAITING/FAILED/STOPPED — 不应该报告，但设置 result 以防万一
            pass

        self._mark_dirty()
        return True

    def approve_plan(self, run_id: str, feedback: str = "", model: str | None = None) -> bool:
        """审批通过 plan — 向 agent 发送 approve 消息并恢复执行。"""
        run_info = self._registry.runs.get(run_id)
        if not run_info or run_info.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() if feedback.strip() else "Approved. Please proceed with the implementation."
        self._transition(run_info, RunStatus.RUNNING)
        ok = self.continue_run(run_id, prompt=msg, source="user", model=model)
        if ok:
            logger.info(f"[{run_id[:8]}] Plan approved: {msg[:60]}")
        else:
            logger.warning(f"[{run_id[:8]}] Plan approved but continue_run failed")
            self._transition(run_info, RunStatus.PLAN_PENDING)  # 回退状态
        return ok

    def reject_plan(self, run_id: str, feedback: str = "", model: str | None = None) -> bool:
        """拒绝 plan — 向 agent 发送 reject 消息，agent 将修改计划。"""
        run_info = self._registry.runs.get(run_id)
        if not run_info or run_info.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() if feedback.strip() else "Plan rejected. Please revise the approach."
        self._transition(run_info, RunStatus.RUNNING)
        logger.info(f"[{run_id[:8]}] Plan rejected: {msg[:60]}")
        return self.continue_run(run_id, prompt=msg, source="user", model=model)

    # endregion


    # region Agent 交互
    def continue_run(self, run_id: str, prompt: str, source: str = "user",
                     model: str | None = None, goal: str | None = None) -> bool:
        """在已有会话上追加一轮对话。可指定 model 覆盖当前会话的模型。"""

        run_info = self._registry.runs.get(run_id)

        if not run_info:
            return False
        if run_info._session and run_info._session.poll() is None:
            # plan_pending 时进程即将退出，terminate 它以便 continue
            if run_info.status == RunStatus.PLAN_PENDING:
                try:
                    run_info._session.terminate()
                    run_info._session.wait(timeout=5)
                except Exception:
                    pass
            else:
                return False
        if not run_info.session_id:
            return False

        # 更新 goal（允许运行时设定/覆盖）
        if goal is not None:
            run_info.goal = goal if goal else None
            run_info.goal_retries = 0  # 重置重试计数
        elif source != "os":
            # 用户手动 continue → 重置 goal 重试计数，给一次新的机会
            run_info.goal_retries = 0

        # sanitize prompt 防止 surrogate 进入内存
        prompt = self._sanitize_unicode(prompt)

        # 模型选择：优先用参数指定的，再用 run_info 原有的，最后用默认值
        effective_model = model if model is not None else (run_info.model or self.default_model)

        run_info.turn_markers.append((len(run_info.output_events), prompt))
        # 结构化事件
        run_info.add_event("turn", index=len(run_info.turn_markers))
        run_info.add_event("prompt", text=prompt, role="user", source=source)

        logger.info(f"[{run_id[:8]}] continue_run: resume session={run_info.session_id[:13]}, "
                    f"system_prompt={'YES' if run_info.system_prompt else 'NONE'} "
                    f"(len={len(run_info.system_prompt) if run_info.system_prompt else 0})")

        # resume 时复用 run_info 原有的 workspace_path，不创建新 workspace
        env = os.environ.copy()
        env.update({
            "AGENT_OS_RUN_ID": run_id,
            "AGENT_OS_PORT": str(self.port),
        })
        if run_info.workspace_path:
            env["AGENT_OS_WORKSPACE"] = run_info.workspace_path
            workspace_cwd = self.project_root
        else:
            env = build_agent_env(run_id, self.project_root, self.port)
            workspace_cwd = self.project_root
        handle = self._backend.launch(
            prompt=prompt,
            model=effective_model,
            session_id=None,
            resume_session=run_info.session_id,
            system_prompt=run_info.system_prompt,
            cwd=workspace_cwd,
            env=env,
        )

        self._transition(run_info, RunStatus.RUNNING)
        run_info.completed_at = None
        run_info.exit_code = None
        object.__setattr__(run_info, '_session', handle)
        object.__setattr__(run_info, '_new_output_event', threading.Event())
        object.__setattr__(run_info, '_bus', self._bus)

        self._stream_reader.start_reader(run_info)
        self._mark_dirty()
        return True

    # endregion


    # region Agent 控制
    def stop_run(self, run_id: str) -> bool:
        """终止子进程。"""
        run_info = self._registry.runs.get(run_id)
        if not run_info or not run_info._session:
            return False

        if run_info.status == RunStatus.RUNNING:
            run_info._session.terminate()
            self._transition(run_info, RunStatus.STOPPED)
            run_info.completed_at = datetime.now()

            self._notify_and_save(run_info.run_id)
            return True
        return False

    def delete_run(self, run_id: str, recursive: bool = True) -> int:
        """删除一个 run。RUNNING 的会先 stop。recursive=True 时递归删除所有子孙。
        返回实际删除的数量。"""
        run_info = self._registry.runs.get(run_id)
        if not run_info:
            return 0
        # 先递归删除子节点
        deleted = 0
        if recursive:
            for child_id in list(run_info.children_run_ids):
                deleted += self.delete_run(child_id, recursive=True)
        # 自己 — 如果还在跑，先 stop
        if run_info.status == RunStatus.RUNNING and run_info._session:
            try:
                run_info._session.terminate()
            except Exception:
                pass
        # 从父节点的 children 列表移除
        if run_info.parent_run_id:
            parent = self._registry.runs.get(run_info.parent_run_id)
            if parent and run_id in parent.children_run_ids:
                parent.children_run_ids.remove(run_id)
        # 从 spawn_requests 里清理引用
        for sr in list(self._registry.spawn_requests.values()):
            if run_id in sr.child_run_ids:
                try:
                    sr.child_run_ids.remove(run_id)
                except ValueError:
                    pass
            sr.completed_children.discard(run_id)
        # 从 SQLite 持久化层删除
        try:
            conn = getattr(self, '_db_conn', None)
            if conn:
                conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
                conn.commit()
        except Exception:
            pass
        del self._registry.runs[run_id]
        return deleted + 1

    # ---------- rewind ----------

    # endregion


    # region 会话管理
    def rewind_to(self, run_id: str, target_seq: int) -> dict:
        ri = self._registry.runs.get(run_id)
        if not ri:
            return {"ok": False, "error": "run not found"}
        result = self._session_manager.rewind_to(ri, target_seq)
        if result.get("ok"):
            self._transition(ri, RunStatus.STOPPED)

            self._notify_and_save(ri.run_id)

        return result

    def handle_send(self, run_id: str, msg: str) -> bool:
        """处理 send.py 发来的消息。会处理 supervisor 审查裁决。"""
        ri = self._registry.runs.get(run_id)
        if not ri:
            return False
        ri.messages.append({"time": datetime.now().isoformat(), "msg": msg})
        ri.add_event("send", text=msg)

        waiting_sup_id = getattr(ri, '_waiting_supervisor', None)
        graph_active = getattr(ri, '_supervisor_graph_active', False)
        if not waiting_sup_id and not graph_active:
            return True

        msg_upper = msg.strip().upper()
        if graph_active:
            # SupervisorGraph 模式：resume graph with verdict
            if msg_upper.startswith("PASS"):
                ri.add_event("system", text="[Agent OS] Supervisor: PASS — task complete")
                finished = self._supervisor_graph.resume_supervisor(run_id, "PASS")
                if not finished:
                    self._mark_dirty()
                    return True
                ri.supervisor = None
                self._orchestrator.on_run_completed(ri)
            elif msg_upper.startswith("CORRECTION"):
                ri.add_event("system", text=f"[Agent OS] Supervisor correction: {msg[:200]}")
                finished = self._supervisor_graph.resume_supervisor(run_id, msg.strip())
                if not finished:
                    self._mark_dirty()
                    return True
        else:
            # fallback：旧逻辑（_waiting_supervisor）
            if msg_upper.startswith("PASS"):
                object.__setattr__(ri, '_waiting_supervisor', None)
                ri.supervisor = None
                ri.add_event("system", text="[Agent OS] Supervisor: PASS — task complete")
                self._orchestrator.on_run_completed(ri)
            elif msg_upper.startswith("CORRECTION"):
                object.__setattr__(ri, '_waiting_supervisor', None)
                ri.add_event("system", text=f"[Agent OS] Supervisor correction: {msg[:200]}")
                self.continue_run(run_id, msg.strip(), source="os")
        self._mark_dirty()
        return True

    def clear_context(self, run_id: str) -> dict:
        ri = self._registry.runs.get(run_id)
        if not ri:
            return {"ok": False, "error": "run not found"}
        result = self._session_manager.clear_context(ri)
        if result.get("ok"):
            self._transition(ri, RunStatus.STOPPED)
            self._notify_and_save(ri.run_id)
        return result

    # endregion


    # region 管理 & DAG
    def dag_checkout(self, run_id: str, step_id: str,
                     rerun_downstream: bool = False) -> dict:
        result = self._dag_service.dag_checkout(run_id, step_id, rerun_downstream)
        if not result.get("ok"):
            return result
        # 从内存中清除该 workspace 的子 agent run（状态随 git 回退已失效）
        ws = self._dag_service._resolve_workspace(run_id)
        if ws:
            to_remove = [

                rid for rid, ri in self._registry.runs.items()

                if ri.workspace_path == ws and ri.parent_run_id is not None
            ]
            for rid in to_remove:
                del self._registry.runs[rid]
        self._mark_dirty()
        return result

    def dag_status_by_workspace(self, workspace_id: str) -> dict:
        return self._dag_service.dag_status_by_workspace(workspace_id)

    def dag_status(self, run_id: str) -> dict:
        return self._dag_service.dag_status(run_id)

    def clear_completed(self) -> int:
        """清理所有已完成的根 run（含其子树）。返回删除的根数量。"""
        roots = [ri for ri in self._registry.runs.values()
                 if not ri.parent_run_id and ri.status in (
                     RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED)]
        count = 0
        for r in roots:
            if self.delete_run(r.run_id, recursive=True) > 0:
                count += 1
        return count

    def list_runs(self) -> list[dict]:
        """列出所有 run 的摘要。"""
        return self._registry.list_runs()

    def get_tree(self) -> list[dict]:
        """返回 agent 树结构（只返回根节点，children 嵌套）。"""
        return self._registry.get_tree()

    def get_run(self, run_id: str) -> RunInfo | None:
        return self._registry.get(run_id)

    async def stream_output(self, run_id: str) -> AsyncGenerator[str, None]:
        """异步生成器：yield 新结构化事件（JSON 字符串）。SSE 协议。"""
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



    MAX_GOAL_RETRIES = 5

    # endregion


    # region Goal / Supervisor
    def set_goal(self, run_id: str, goal: str, max_retries: int | None = None) -> bool:
        """为此 run 设置 goal。可指定 max_retries（None=使用全局默认 MAX_GOAL_RETRIES）。"""
        run_info = self._registry.runs.get(run_id)
        if not run_info:
            return False
        run_info.goal = goal
        run_info.goal_retries = 0
        # 动态覆盖每个 run 的最大重试次数
        if max_retries is not None:
            run_info._max_goal_retries = max_retries
        logger.info(f"[{run_id[:8]}] set_goal: \"{goal}\" (max_retries={max_retries})")

        return True

    def skip_goal(self, run_id: str) -> bool:
        """停止该 run 的 goal 评估重试循环。"""
        run_info = self._registry.runs.get(run_id)
        if not run_info:
            return False
        run_info.goal_retries = getattr(run_info, '_max_goal_retries', self.MAX_GOAL_RETRIES)
        logger.info(f"[{run_id[:8]}] skip_goal: goal retries maxed out, goal evaluation disabled")
        return True


    def _try_record_step_completion(self, run_info: RunInfo, result: str) -> None:
        """标记 DAG step 完成 + 记忆层记录（幂等，仅首次生效）。"""
        # 1. DAG 状态更新
        if run_info.step_id and run_info.workspace_path:
            try:
                dag = dp.load_dag(run_info.workspace_path)
                steps = dag.get("steps", [])
                if dp.mark_done(steps, run_info.step_id):
                    dp.save_dag(run_info.workspace_path, dag)
                    logger.info(f"[{run_info.run_id[:8]}] DAG step marked done: {run_info.step_id}")
            except Exception as e:
                logger.warning(f"[{run_info.run_id[:8]}] DAG mark_done failed: {e}")


    # endregion



