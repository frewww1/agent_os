"""Claude CLI 子进程管理器 — 启动、流式输出、会话管理、父子关系、自动 resume。"""
import asyncio
import json as _json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from typing import AsyncGenerator


from . import dag_planner as dp
from ..persistence.git_recorder import Recorder
from .models import RunStatus, RunInfo, SpawnRequest
from ..utils import sanitize, sanitize_workspace_name, cwd_to_session_key
from ..persistence.sqlite import serialize_run, save_runs_to_disk, load_runs_from_disk
from .stream_parser import extract_session_id, parse_stream_json_events
from ..agent.backend import get_backend, SessionHandle


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


class ProcessManager:
    """管理多个 claude CLI 子进程，支持父子嵌套和自动 resume。"""

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
        logger.info(f"ProcessManager: backend={type(self._backend).__name__}")
        self.runs: dict[str, RunInfo] = {}
        self.spawn_requests: dict[str, SpawnRequest] = {}
        self._resume_callback = None  # set by main.py for async resume
        self._models_cache: list[str] | None = None  # `<cli> --help` 解析出的模型列表缓存
        self._originally_waiting: set[str] = set()  # 重启前状态为 WAITING 的 run_id
        self.recorder = Recorder(project_root=self.project_root)  # workspace 记忆层

        # asyncio 事件循环引用（供后台任务使用）
        self._loop = loop or asyncio.get_event_loop()

        # 持久化文件
        self._state_dir = os.path.join(PROJECT_ROOT, "state")
        os.makedirs(self._state_dir, exist_ok=True)
        self._runs_file = os.path.join(self._state_dir, "runs.json")
        # 启动时尝试恢复历史 runs（仅元数据 + 事件流；进程引用全部丢失，
        # 这些 run 之后都是只读的，能查看 / 删除 / 导出但不能 resume）
        load_runs_from_disk(self)
        # 迁移旧位置 workspaces/<ws>/state/ → state/workspaces/<ws>/
        self._migrate_legacy_workspace_state()
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

    async def _timeout_watcher(self):
        """每 30s 扫一次所有 RUNNING 的子 agent，超过 idle_timeout 强制结束。"""
        while True:
            await asyncio.sleep(30.0)
            try:
                now = datetime.now()
                victims = []
                for ri in list(self.runs.values()):
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
                    ri = self.runs.get(run_id)
                    if ri:
                        ri.add_text_line(
                            f"[Agent OS] Auto-ended: idle for {int(idle_sec)}s (> {self._idle_timeout_sec}s timeout)",
                            kind="error",
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
                count = len(self.runs)
                save_runs_to_disk(self)
                logger.info(f"persist: saved {count} runs to disk")
            except Exception as e:
                logger.warning(f"persist worker error: {e}")

    def _mark_dirty(self):
        """标记需要持久化（线程安全）。"""
        self._save_dirty = True

    def _restore_spawn_requests(self):
        """从已加载的 runs 中重建 spawn_requests。
        通过 parent_run_id 分组：同一个父 agent 的所有子 agent 属于同一个 spawn 请求。
        spawn_id 从父 agent 的 spawn_id 获取。"""
        from collections import defaultdict
        # 按 parent_run_id 分组子 agent
        parent_groups = defaultdict(list)
        for run_id, ri in self.runs.items():
            if ri.parent_run_id:
                parent_groups[ri.parent_run_id].append(run_id)
        
        for parent_id, child_ids in parent_groups.items():
            if not child_ids:
                continue
            parent = self.runs.get(parent_id)
            # 过滤掉 supervisor 子 agent（不是 spawn 子 agent）
            active_sup = getattr(parent, '_active_supervisor', None) if parent else None
            child_ids = [cid for cid in child_ids if cid != active_sup]
            if not child_ids:
                continue
            spawn_id = parent.spawn_id if parent else ""
            if not spawn_id:
                # 父 agent 没有 spawn_id（可能是旧数据），生成一个
                spawn_id = f"restored_{parent_id[:8]}"
                if parent:
                    parent.spawn_id = spawn_id
            
            parent_session = parent.session_id if parent else ""
            
            # 统计已完成的子 run
            # STOPPED 状态的子 agent：可能在重启前已完成（有结果）或被强制
            # 中止（无结果）。只有有结果的才算"完成"，无结果的视为未完成，
            # 等子 agent 真正完成后由 _on_run_completed 正常触发 resume。
            completed = set()
            for cid in child_ids:
                child = self.runs.get(cid)
                if not child:
                    continue
                if child.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    completed.add(cid)
                elif child.status == RunStatus.STOPPED:
                    # STOPPED 是重启强制改为 stopped 的。
                    # 只有 reported_result（report.py 调用过）才算真正完成。
                    # _fallback_result/messages 可能是上个 session 残留，不可信。
                    if child.reported_result:
                        completed.add(cid)
            
            spawn_req = SpawnRequest(
                spawn_id=spawn_id,
                parent_run_id=parent_id,
                parent_session_id=parent_session,
                child_run_ids=child_ids,
                wait_strategy="all",
            )
            spawn_req.completed_children = completed
            # 如果所有子 run 都已完成，标记为已解决。
            # 但如果子 agent 仅因重启被强制置为 stopped（无实际结果），
            # 则不标记 resolved —— 等子 agent 真正完成后由 _on_run_completed 触发 resume。
            if len(completed) == len(child_ids):
                child_has_result = lambda cid: (
                    (c := self.runs.get(cid)) and (
                        c.reported_result  # 只有 report.py 调过才算真正完成
                    )
                )
                if any(child_has_result(cid) for cid in child_ids):
                    spawn_req.is_resolved = True
            logger.debug(
                f"_restore_spawn_requests: {spawn_id[:10]} parent={parent_id[:8]} "
                f"children={[c[:8] for c in child_ids]} "
                f"completed={[c[:8] for c in completed]} resolved={spawn_req.is_resolved}"
            )
                # else: 子 agent 被强制 stopped 但无结果，不标记 resolved
            self.spawn_requests[spawn_id] = spawn_req
            
        if parent_groups:
            logger.info(f"restored {len(parent_groups)} spawn requests from historical runs")

    def _migrate_legacy_workspace_state(self):
        """将旧位置的 OS 文件从 workspace 内搬到 state/ 下，避免 agent 读到。

        迁移内容：
        - workspaces/<ws>/state/runs.json → state/workspaces/<ws>/runs.json
        - workspaces/<ws>/runs/<run_id>/ → state/records/<ws>/<run_id>/
        """
        import shutil
        workspaces_dir = os.path.join(self.project_root, ".agent_os", "workspaces")
        if not os.path.isdir(workspaces_dir):
            return
        migrated_state = 0
        migrated_runs = 0
        for ws_name in os.listdir(workspaces_dir):
            ws_path = os.path.join(workspaces_dir, ws_name)
            # 1) 迁移 state/runs.json
            old_state_file = os.path.join(ws_path, "state", "runs.json")
            if os.path.exists(old_state_file):
                new_dir = os.path.join(self._state_dir, "workspaces", ws_name)
                new_file = os.path.join(new_dir, "runs.json")
                try:
                    if os.path.exists(new_file):
                        os.remove(old_state_file)
                    else:
                        os.makedirs(new_dir, exist_ok=True)
                        os.replace(old_state_file, new_file)
                    # 清理空 state/ 目录
                    old_state_dir = os.path.dirname(old_state_file)
                    if os.path.isdir(old_state_dir) and not os.listdir(old_state_dir):
                        os.rmdir(old_state_dir)
                    migrated_state += 1
                except OSError as e:
                    logger.warning(f"migrate state for {ws_name}: {e}")
            # 2) 迁移 runs/<run_id>/record.json
            old_runs_dir = os.path.join(ws_path, "runs")
            if os.path.isdir(old_runs_dir):
                new_records_dir = os.path.join(self._state_dir, "records", ws_name)
                for run_id in os.listdir(old_runs_dir):
                    old_run_dir = os.path.join(old_runs_dir, run_id)
                    new_run_dir = os.path.join(new_records_dir, run_id)
                    if not os.path.isdir(old_run_dir):
                        continue
                    try:
                        if not os.path.exists(new_run_dir):
                            os.makedirs(os.path.dirname(new_run_dir), exist_ok=True)
                            shutil.move(old_run_dir, new_run_dir)
                        else:
                            shutil.rmtree(old_run_dir)
                        migrated_runs += 1
                    except OSError as e:
                        logger.warning(f"migrate runs/{run_id} for {ws_name}: {e}")
                # 清理空 runs/ 目录
                try:
                    if not os.listdir(old_runs_dir):
                        os.rmdir(old_runs_dir)
                except OSError:
                    pass
        if migrated_state:
            logger.info(f"migrated {migrated_state} legacy workspace state files")
        if migrated_runs:
            logger.info(f"migrated {migrated_runs} legacy run record dirs to state/records/")

    def _resume_restored_parents(self):
        """重启后恢复已完成的父 agent（子 agent 在重启前已通过 Done/report.py 完成）。

        仅恢复 _restore_spawn_requests 标记为已解决的 spawn 请求。
        不解决的情况（子 agent 仅因重启被强制 stopped，无实际结果）由
        用户操作（Done 点击/report.py 调用）触发 _on_run_completed 正常 resume。
        """
        resumed_count = 0
        for spawn_id, spawn_req in list(self.spawn_requests.items()):
            # 只有 genuinely resolved 的 spawn 请求才恢复
            # （_restore_spawn_requests 仅在子 agent 有实际结果时标记 resolved）
            if not spawn_req.is_resolved:
                continue

            parent_id = spawn_req.parent_run_id
            parent = self.runs.get(parent_id)
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
        """启动新 claude 会话，返回 run_id。

        workspace_name: 可选。根 agent 的工作目录命名。传入时复用/创建
            ``workspaces/<workspace_name>``（按名字跨 run 持久化复用）；
            不传则沿用默认的 ``workspaces/<run_id>``。子 agent 始终继承父目录，
            不受该参数影响。
        """
        # sanitize 入口处清除 surrogate，防止污染内存和序列化
        prompt = prompt.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        if system_prompt:
            system_prompt = system_prompt.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        run_id = uuid.uuid4().hex[:10]
        # OS 主动生成 session_id 并通过 --session-id 注入，不再依赖 stream-json 输出。
        # 这样即使 claude CLI 在某些情况下回退到非 JSON 输出，OS 也能可靠地 resume。
        session_id = str(uuid.uuid4())
        # 如果没有指定 model，用 ProcessManager 的默认 model
        if model is None:
            model = self.default_model
        logger.info(f"start_run: run_id={run_id}, session_id={session_id[:13]}, "
                    f"prompt={prompt[:50]}, interactive={interactive}, model={model}, "
                    f"goal={goal}")

        # 构建环境变量（让子 agent 知道自己的 run_id，以便嵌套 spawn）
        env = self._build_env(run_id, workspace_name=workspace_name, env_extras=env_extras)
        if env_extras:
            env.update(env_extras)
        # DEBUG: 确认 step_id 是否传递
        step_id_from_env = env.get("AGENT_OS_STEP_ID")
        if step_id_from_env:
            logger.info(f"[{run_id[:8]}] STEP_ID from env: {step_id_from_env}")

        # 根 agent 注入 Agent OS system prompt（env 已构建，可获取真实 workspace 路径）
        if not parent_run_id and not system_prompt:
            system_prompt = self._build_root_system_prompt(
                env.get("AGENT_OS_WORKSPACE", ".agent_os/workspaces/<run>/"))

        logger.info(f"[{run_id[:8]}] Launching agent...")
        # CLI 启动路径统一为 project_root（.agent_os 的上一级）
        workspace_cwd = self.project_root
        try:
            handle = self._backend.launch(
                prompt=prompt,
                model=model,
                session_id=session_id,
                resume_session=None,
                system_prompt=system_prompt,
                cwd=workspace_cwd,
                env=env,
            )
        except Exception as e:
            logger.error(f"[{run_id[:8]}] Launch failed: {e}")
            run_info = RunInfo(run_id=run_id, prompt=prompt, status=RunStatus.FAILED)
            run_info.add_text_line(f"[ERROR] Popen failed: {e}", kind="error")
            self.runs[run_id] = run_info
            self._mark_dirty()
            return run_id

        loop = self._loop  # 使用主事件循环，避免 reader 线程无 loop 报错
        # 计算深度：根 agent depth=0，子 agent 继承父 depth+1
        _depth = 0
        if parent_run_id and parent_run_id in self.runs:
            _depth = (getattr(self.runs[parent_run_id], '_depth', 0) or 0) + 1

        run_info = RunInfo(
            run_id=run_id,
            prompt=prompt,
            session_id=session_id,
            parent_run_id=parent_run_id,
            interactive=interactive,
            model=model,
            task_type=task_type,
            workspace_path=env.get("AGENT_OS_WORKSPACE"),
            step_id=env.get("AGENT_OS_STEP_ID"),
            system_prompt=system_prompt,
            goal=goal,
            supervisor=supervisor,
        )
        # 运行时字段（pydantic 不管理，直接设置）
        object.__setattr__(run_info, '_session', handle)
        object.__setattr__(run_info, '_new_output_event', threading.Event())
        object.__setattr__(run_info, '_loop', loop)
        object.__setattr__(run_info, '_dirty_callback', self._mark_dirty)
        run_info._depth = _depth
        run_info.turn_markers.append((0, prompt))
        self.runs[run_id] = run_info
        # 结构化事件：Turn 1 开始 + 用户 prompt
        run_info.add_event("turn", index=1)
        run_info.add_event("prompt", text=prompt, role="user", source="user")

        # 记忆层：记录 run 开始
        if run_info.workspace_path:
            try:
                self.recorder.run_start(
                    run_id=run_id,
                    agent_name=agent_name or f"agent-{run_id[:6]}",
                    prompt=prompt,
                    workspace_path=run_info.workspace_path,
                    is_root=(parent_run_id is None),
                )
            except Exception:
                pass

        # 如果有父节点，记录在父节点的 children 中
        if parent_run_id and parent_run_id in self.runs:
            self.runs[parent_run_id].children_run_ids.append(run_id)

        # 写 marker 文件到工作目录，让 spawn.py 能找到 parent_run_id
        marker_path = os.path.join(self.project_root, ".agent_os_run_id")
        try:
            with open(marker_path, "w") as f:
                f.write(run_id)
        except Exception:
            pass

        self._start_reader(run_info)
        self._mark_dirty()
        return run_id

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
            parent = self.runs.get(parent_run_id)
            logger.debug(f"spawn_children lookup: parent_run_id={parent_run_id}, found={parent is not None}, "
                         f"session_id={parent.session_id if parent else 'N/A'}")
            if parent and parent.session_id:
                parent_session_id = parent.session_id

        # 计算深度并做限制
        depth = 0
        if parent_run_id and parent_run_id in self.runs:
            p = self.runs[parent_run_id]
            depth = (getattr(p, '_depth', 0) or 0) + 1

        # 最多 3 层：根 agent(0) → 子 agent(1) → 孙 agent(2)
        if depth >= 3:
            logger.warning(f"spawn_children: depth={depth} >= 3, rejecting from {parent_run_id[:8]}")
            return {"spawn_id": "", "child_count": 0, "child_run_ids": [],
                    "error": f"max depth 3 exceeded (depth={depth})"}

        # 探索模式 agent 禁止 spawn
        if parent_run_id and parent_run_id in self.runs:
            p = self.runs[parent_run_id]
            if getattr(p, 'task_type', 'generative') == 'explore':
                logger.warning(f"spawn_children: explore agent {parent_run_id[:8]} cannot spawn")
                return {"spawn_id": "", "child_count": 0, "child_run_ids": [],
                        "error": "explore agent cannot spawn children"}

        # 父 agent 的 model 作为子 agent 默认值
        parent_model = None
        parent_workspace = None
        if parent_run_id and parent_run_id in self.runs:
            parent = self.runs[parent_run_id]
            parent_model = parent.model
            # 取父 agent 的真实 workspace，让子 agent 共享同一目录。
            # 必须用 parent.workspace_path 而非 workspaces/<parent_run_id>：
            # 当父本身是被 spawn 的子 agent 时，它的 workspace 是从更上层继承来的
            # （目录名是祖先的 run_id），按 parent_run_id 拼路径会指向不存在的目录，
            # 导致孙 agent 另起新 workspace，断开嵌套 spawn 的「同树共享」。
            parent_workspace = parent.workspace_path or os.path.join(
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
            task_type = task.get("type") or task.get("agent_type") or task.get("subagent_type") or "generative"
            child_model = task.get("model") or parent_model
            step_id = task.get("step_id")  # DAG step 标识，透传给子 agent env
            # 如果调度 agent 没传 type，从 dag.json 自动补全
            if step_id and parent_workspace and not task.get("type") and not task.get("agent_type") and not task.get("subagent_type"):
                try:
                    dag = dp.load_dag(parent_workspace)
                    for s in dag.get("steps", []):
                        if s.get("id") == step_id:
                            dag_type = s.get("type", "generative")
                            if dag_type != task_type:
                                task_type = dag_type
                                logger.info(f"spawn_children: auto-filled type={task_type} for step {step_id}")
                            break
                except Exception:
                    pass
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
            sub_system_prompt = self._build_subagent_system_prompt(
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
        self.spawn_requests[spawn_id] = spawn_req

        # 标记父 agent 为 WAITING
        if parent_run_id and parent_run_id in self.runs:
            parent = self.runs[parent_run_id]
            parent.status = RunStatus.WAITING
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
        run_info = self.runs.get(run_id)
        if not run_info:
            return False
        if run_info.status not in (RunStatus.RUNNING,):
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
        run_info.status = RunStatus.COMPLETED
        run_info.completed_at = datetime.now()
        kind_label = "Interactive" if run_info.interactive else "Generative"
        run_info.add_text_line(f"[{kind_label} Agent] Ended by user (Done).", kind="system")
        # 用单独的事件类型，避免和子 agent 自己 report.py 的"final result"混淆
        run_info.add_event("user_done")

        # DAG 状态更新：标记 step 为 done（必须在 step_done 之前，确保 dag.json 变更被包含在 commit 中）
        if run_info.step_id and run_info.workspace_path:
            try:
                dag = dp.load_dag(run_info.workspace_path)
                steps = dag.get("steps", [])
                if dp.mark_done(steps, run_info.step_id):
                    dp.save_dag(run_info.workspace_path, dag)
                    logger.info(f"[{run_id[:8]}] DAG step marked done: {run_info.step_id}")
            except Exception as e:
                logger.warning(f"[{run_id[:8]}] DAG mark_done failed: {e}")

        # 记忆层：记录 run 被用户手动结束（仅首次）
        if run_info.workspace_path and not run_info._recorded:
            run_info._recorded = True
            try:
                if run_info.step_id:
                    # DAG step：打 [step:<id>] commit（dag.json 已更新，commit 会包含状态变更）
                    self.recorder.step_done(
                        run_id=run_id,
                        step_id=run_info.step_id,
                        workspace_path=run_info.workspace_path,
                        message="(用户手动结束)",
                    )
                    self.recorder.run_done(
                        run_id=run_id,
                        result="(用户手动结束)",
                        workspace_path=run_info.workspace_path,
                        do_commit=False,
                    )
                else:
                    self.recorder.run_done(
                        run_id=run_id,
                        result="(用户手动结束)",
                        workspace_path=run_info.workspace_path,
                    )
            except Exception:
                pass

        # 唤醒 SSE
        if run_info._loop and run_info._new_output_event:
            try:
                run_info._new_output_event.set()
            except RuntimeError:
                pass

        # 触发 resume 检查
        self._on_run_completed(run_info)
        self._mark_dirty()
        return True

    def report_complete(self, run_id: str, result: str) -> bool:
        """子 agent 调用 report.py 汇报结果。

        generative: 设置 result 并触发 resume。
        interactive: 忽略 — interactive agent 不应调用 report.py，由用户点 Done 结束。
        """
        run_info = self.runs.get(run_id)
        if not run_info:
            return False

        # interactive agent 调用 report.py → 忽略
        if run_info.interactive:
            logger.info(f"[{run_id[:8]}] Interactive agent called report.py — ignored, waiting for user Done")
            return True

        # supervisor agent 调 report.py → 用上报结果唤醒等待中的执行 agent
        if run_info.parent_run_id:
            parent_ri = self.runs.get(run_info.parent_run_id)
            if parent_ri and getattr(parent_ri, '_waiting_supervisor', None) == run_id:
                result = result.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                run_info.reported_result = result
                run_info.status = RunStatus.COMPLETED
                run_info.completed_at = datetime.now()

                msg_upper = result.strip().upper()
                object.__setattr__(parent_ri, '_waiting_supervisor', None)

                if msg_upper.startswith("PASS"):
                    object.__setattr__(run_info, '_supervisor_done', True)
                    object.__setattr__(parent_ri, '_active_supervisor', None)
                    parent_ri.supervisor = None
                    parent_ri.add_text_line("[Agent OS] Supervisor: PASS — task complete", kind="system")
                    logger.info(f"[{parent_ri.run_id[:8]}] Supervisor PASS")
                    self._on_run_completed(parent_ri)
                elif msg_upper.startswith("CORRECTION"):
                    parent_ri.add_text_line(f"[Agent OS] Supervisor correction: {result[:200]}", kind="system")
                    logger.info(f"[{parent_ri.run_id[:8]}] Supervisor CORRECTION, resuming")
                    self.continue_run(parent_ri.run_id, result, source="os")
                else:
                    parent_ri.add_text_line(f"[Agent OS] Supervisor feedback: {result[:200]}", kind="system")
                    self.continue_run(parent_ri.run_id, result, source="os")

                self._mark_dirty()
                return True

        result = result.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        run_info.reported_result = result
        logger.info(f"[{run_id[:8]}] report_complete: status={run_info.status.value}, result={result[:50]}")

        # DAG 状态更新：标记 step 为 done（必须在 step_done 之前）
        if run_info.step_id and run_info.workspace_path:
            try:
                dag = dp.load_dag(run_info.workspace_path)
                steps = dag.get("steps", [])
                if dp.mark_done(steps, run_info.step_id):
                    dp.save_dag(run_info.workspace_path, dag)
            except Exception as e:
                logger.warning(f"[{run_id[:8]}] DAG mark_done failed: {e}")

        # 记忆层：记录 run 完成（仅首次）
        if run_info.workspace_path and not run_info._recorded:
            run_info._recorded = True
            try:
                if run_info.step_id:
                    # DAG step：先打 [step:<id>] commit（包含文件变更）
                    self.recorder.step_done(
                        run_id=run_id,
                        step_id=run_info.step_id,
                        workspace_path=run_info.workspace_path,
                        message=result[:80],
                    )
                    self.recorder.run_done(
                        run_id=run_id,
                        result=result,
                        workspace_path=run_info.workspace_path,
                        do_commit=False,
                    )
                else:
                    self.recorder.run_done(
                        run_id=run_id,
                        result=result,
                        workspace_path=run_info.workspace_path,
                    )
            except Exception:
                pass

        if run_info.status == RunStatus.RUNNING:
            # 先终止进程，确保后续 _on_run_completed → continue_run 时进程已退出
            proc = run_info._session
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            run_info.status = RunStatus.COMPLETED
            run_info.completed_at = datetime.now()
            run_info.add_event("report", text=result)
            self._on_run_completed(run_info)
        elif run_info.status == RunStatus.COMPLETED:
            # 进程已经退出但 _on_run_completed 可能已经触发过了
            # 需要再次检查是否所有子任务都完成了（因为这次有了 reported_result）
            self._on_run_completed(run_info)
        else:
            # WAITING/FAILED/STOPPED — 不应该报告，但设置 result 以防万一
            pass

        self._mark_dirty()
        return True

    @staticmethod
    def _find_latest_plan_file() -> str | None:
        """返回 ~/.codebuddy/plans/ 下最近修改的 .md 文件路径，找不到则 None。"""
        import pathlib
        plans_dir = pathlib.Path.home() / ".codebuddy" / "plans"
        if not plans_dir.is_dir():
            return None
        mds = sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        return str(mds[0]) if mds else None

    def approve_plan(self, run_id: str, feedback: str = "", model: str | None = None) -> bool:
        """审批通过 plan — 向 agent 发送 approve 消息并恢复执行。"""
        run_info = self.runs.get(run_id)
        if not run_info or run_info.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() if feedback.strip() else "Approved. Please proceed with the implementation."
        run_info.status = RunStatus.RUNNING
        ok = self.continue_run(run_id, prompt=msg, source="user", model=model)
        if ok:
            logger.info(f"[{run_id[:8]}] Plan approved: {msg[:60]}")
        else:
            logger.warning(f"[{run_id[:8]}] Plan approved but continue_run failed")
            run_info.status = RunStatus.PLAN_PENDING  # 回退状态
        return ok

    def reject_plan(self, run_id: str, feedback: str = "", model: str | None = None) -> bool:
        """拒绝 plan — 向 agent 发送 reject 消息，agent 将修改计划。"""
        run_info = self.runs.get(run_id)
        if not run_info or run_info.status != RunStatus.PLAN_PENDING:
            return False
        msg = feedback.strip() if feedback.strip() else "Plan rejected. Please revise the approach."
        run_info.status = RunStatus.RUNNING
        logger.info(f"[{run_id[:8]}] Plan rejected: {msg[:60]}")
        return self.continue_run(run_id, prompt=msg, source="user", model=model)

    def continue_run(self, run_id: str, prompt: str, source: str = "user",
                     model: str | None = None, goal: str | None = None) -> bool:
        """在已有会话上追加一轮对话。可指定 model 覆盖当前会话的模型。"""
        run_info = self.runs.get(run_id)
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
        prompt = prompt.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

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
            env = self._build_env(run_id)
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

        run_info.status = RunStatus.RUNNING
        run_info.completed_at = None
        run_info.exit_code = None
        object.__setattr__(run_info, '_session', handle)
        object.__setattr__(run_info, '_new_output_event', threading.Event())

        self._start_reader(run_info)
        self._mark_dirty()
        return True

    def stop_run(self, run_id: str) -> bool:
        """终止子进程。"""
        run_info = self.runs.get(run_id)
        if not run_info or not run_info._session:
            return False

        if run_info.status == RunStatus.RUNNING:
            run_info._session.terminate()
            run_info.status = RunStatus.STOPPED
            run_info.completed_at = datetime.now()

            # 记忆层：记录被停止
            if run_info.workspace_path and not run_info._recorded:
                run_info._recorded = True
                try:
                    self.recorder.run_done(
                        run_id=run_id,
                        result="(已被手动停止)",
                        workspace_path=run_info.workspace_path,
                    )
                except Exception:
                    pass

            if run_info._loop and run_info._new_output_event:
                run_info._new_output_event.set()
            self._mark_dirty()
            return True
        return False

    def delete_run(self, run_id: str, recursive: bool = True) -> int:
        """删除一个 run。RUNNING 的会先 stop。recursive=True 时递归删除所有子孙。
        返回实际删除的数量。"""
        run_info = self.runs.get(run_id)
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
            parent = self.runs.get(run_info.parent_run_id)
            if parent and run_id in parent.children_run_ids:
                parent.children_run_ids.remove(run_id)
        # 从 spawn_requests 里清理引用
        for sr in list(self.spawn_requests.values()):
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
        del self.runs[run_id]
        return deleted + 1

    # ---------- rewind ----------

    def rewind_to(self, run_id: str, target_seq: int) -> dict:
        """回退会话到 seq=target_seq 的 user prompt 之前（不含该 prompt）。

        副作用：
          1. 截断 jsonl：删除 target 这条 user 消息及之后的所有行
          2. 截断 RunInfo.output_events：保留 seq < target_seq 的事件
          3. 同步截断 turn_markers / output_lines / messages
          4. 重置 status 为 STOPPED、reported_result 等为 None，等待用户重新输入

        约束：
          - run 必须不是 RUNNING / WAITING（否则会和 CLI 抢同一份 session 文件）
          - 目标事件必须是 kind=prompt 且 source=user
        """
        ri = self.runs.get(run_id)
        if not ri:
            return {"ok": False, "error": "run not found"}
        if ri.status in (RunStatus.RUNNING, RunStatus.WAITING):
            return {"ok": False, "error": f"cannot rewind while status={ri.status.value}; stop the run first"}

        # 找目标事件
        target_ev = None
        for ev in ri.output_events:
            if ev.get("seq") == target_seq:
                target_ev = ev
                break
        if not target_ev:
            return {"ok": False, "error": f"event seq={target_seq} not found"}
        if target_ev.get("kind") != "prompt" or target_ev.get("source") != "user":
            return {"ok": False, "error": "target event is not a user prompt"}
        if not ri.session_id:
            return {"ok": False, "error": "run has no session_id"}

        # 定位 jsonl 文件
        cwd = ri.workspace_path or self.project_root
        jsonl_path = self._backend.get_session_path(ri.session_id, cwd)
        if not jsonl_path:
            return {"ok": False, "error": f"session jsonl not found for session_id={ri.session_id[:8]} cwd={cwd}"}

        # 在 jsonl 中找匹配行：role=user, type=message, content[0].text 完全匹配 prompt 文本，
        # 且 timestamp 在 event.ts ±60s 内。命中多条取最后一条（截断点最靠后 = 保留最多）。
        target_text = target_ev.get("text", "")
        target_ts_ms = None
        try:
            target_ts_ms = int(datetime.fromisoformat(target_ev["ts"]).timestamp() * 1000)
        except Exception:
            pass

        cut_line_idx = None
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            return {"ok": False, "error": f"read jsonl failed: {e}"}

        for idx, raw in enumerate(lines):
            try:
                obj = _json.loads(raw)
            except Exception:
                continue
            if obj.get("role") != "user" or obj.get("type") != "message":
                continue
            content = obj.get("content") or []
            if not content or not isinstance(content, list):
                continue
            first = content[0]
            if not isinstance(first, dict) or first.get("type") != "input_text":
                continue
            if first.get("text", "") != target_text:
                continue
            if target_ts_ms is not None:
                ts = obj.get("timestamp")
                if isinstance(ts, (int, float)) and abs(ts - target_ts_ms) > 60_000:
                    continue
            cut_line_idx = idx  # 持续覆盖，命中多条取最后一条

        if cut_line_idx is None:
            return {"ok": False, "error": "could not locate target prompt in jsonl (text/timestamp mismatch)"}

        # 备份原文件再截断（防呆）
        try:
            backup = jsonl_path + f".rewind-{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
            with open(backup, "w", encoding="utf-8") as f:
                f.writelines(lines)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.writelines(lines[:cut_line_idx])
            logger.info(f"[{run_id[:8]}] rewind: jsonl truncated at line {cut_line_idx}, backup={os.path.basename(backup)}")
        except Exception as e:
            return {"ok": False, "error": f"truncate jsonl failed: {e}"}

        # 截断内存：output_events
        kept = [e for e in ri.output_events if e.get("seq", 0) < target_seq]
        ri.output_events.clear()
        for e in kept:
            ri.output_events.append(e)
        ri._event_seq = max((e.get("seq", 0) for e in kept), default=0)

        # turn_markers：每个 element 是 (line_offset, prompt)；target 是新一轮的开头，
        # 删除等于 target 的那一项以及之后。无可靠 line_offset 映射，按 prompt 文本对账。
        new_markers = []
        for m in ri.turn_markers:
            if isinstance(m, (list, tuple)) and len(m) >= 2 and m[1] == target_text:
                break
            new_markers.append(m)
        ri.turn_markers = new_markers

        # 重置状态：变成 STOPPED，等用户重新输入
        ri.status = RunStatus.STOPPED
        ri.reported_result = None
        ri._fallback_result = None
        ri.user_terminated = False
        ri.exit_code = None
        ri.completed_at = None
        ri._recorded = False
        ri.add_event("rewind", to_seq=target_seq, jsonl_cut_line=cut_line_idx)

        # git 回退：找到该 run 的 agent 级 commit，reset 到它的父 commit
        logger.info(f"[{run_id[:8]}] rewind: workspace_path={ri.workspace_path}")
        if ri.workspace_path:
            try:
                run_sha = self.recorder.run_commit_sha(run_id, ri.workspace_path)
                logger.info(f"[{run_id[:8]}] rewind: agent_sha={run_sha}")
                if run_sha:
                    # reset 到该 run commit 的父 commit（即撤销该 run 的 commit）
                    import subprocess as _sp
                    wdir = ri.workspace_path
                    parent = _sp.run(
                        ["git", "rev-parse", f"{run_sha}~1"],
                        cwd=wdir, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=10
                    )
                    parent_sha = parent.stdout.strip()
                    if parent_sha and parent.returncode == 0:
                        result = self.recorder.reset_to_commit(
                            parent_sha, ri.workspace_path, hard=True
                        )
                        logger.info(f"[{run_id[:8]}] rewind: git reset to {parent_sha[:8]} "
                                   f"(before {run_sha[:8]}), ok={result.get('ok')}")
                        ri.add_event("system", text=f"Git reset to before [{run_id[:8]}] commit")
            except Exception as e:
                logger.warning(f"[{run_id[:8]}] rewind: git reset failed (non-fatal): {e}")

        # 唤醒 SSE 让前端立刻看到状态变化
        if ri._loop and ri._new_output_event:
            try:
                ri._new_output_event.set()
            except RuntimeError:
                pass
        self._mark_dirty()

        return {"ok": True, "cut_seq": target_seq, "jsonl_cut_line": cut_line_idx, "backup": backup}

    def clear_context(self, run_id: str) -> dict:
        """清除当前 run 的对话上下文（类似 Claude Code 的 /clear）。

        副作用：
          1. 清空 CLI session jsonl 文件（备份 .bak）
          2. 清空内存中的 output_events / output_lines / turn_markers / messages
          3. 重置 status 为 STOPPED、reported_result 等为 None
          4. 保留 run 元数据（run_id / session_id / workspace_path / step_id 等）

        约束：run 不能是 RUNNING / WAITING（否则会和 CLI 抢 session 文件）。
        """
        ri = self.runs.get(run_id)
        if not ri:
            return {"ok": False, "error": "run not found"}
        if ri.status in (RunStatus.RUNNING, RunStatus.WAITING):
            return {"ok": False, "error": f"cannot clear while status={ri.status.value}; stop the run first"}
        if not ri.session_id:
            return {"ok": False, "error": "run has no session_id"}

        # 定位 jsonl 文件
        cwd = ri.workspace_path or self.project_root
        jsonl_path = self._backend.get_session_path(ri.session_id, cwd)
        if not jsonl_path:
            return {"ok": False, "error": f"session jsonl not found for session_id={ri.session_id[:8]}"}

        # 备份再清空
        backup = jsonl_path + f".clear-{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
        try:
            import shutil
            shutil.copy2(jsonl_path, backup)
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.write("")  # 清空
            logger.info(f"[{run_id[:8]}] clear_context: jsonl cleared, backup={os.path.basename(backup)}")
        except Exception as e:
            return {"ok": False, "error": f"clear jsonl failed: {e}"}

        # 清空内存事件流
        ri.output_events.clear()
        ri.turn_markers.clear()
        ri.messages.clear()
        ri._event_seq = 0
        ri.reported_result = None
        ri._fallback_result = None
        ri.user_terminated = False
        ri.exit_code = None
        ri.completed_at = None
        ri._recorded = False

        # 重置为 STOPPED，等用户重新输入
        ri.status = RunStatus.STOPPED
        ri.add_event("system", text="Context cleared — ready for new input")

        # 唤醒 SSE
        if ri._loop and ri._new_output_event:
            try:
                ri._new_output_event.set()
            except RuntimeError:
                pass
        self._mark_dirty()
        return {"ok": True, "backup": backup}

    def dag_checkout(self, run_id: str, step_id: str,
                     rerun_downstream: bool = False) -> dict:
        """【回退到任一 agent】把 workspace 文件回退到某 DAG step 完成时的快照，
        并同步把该 step + 下游 DAG 状态重置为 pending。

        这是需求"用 git 管理所有 agent 输出、支持回退到任一 agent"的落地：
          1. git checkout 到该 step 的 `[step:<id>]` commit（recorder.checkout_step）
          2. 把该 step 及所有传递下游在 dag.json 里重置为 pending（dag_planner）
          3. rerun_downstream=False 时只回退+重置状态，等调度 agent 下一轮
             --ready 自然取到这些 pending step 重跑；为 True 仅作语义标记
             （实际重跑仍由调度 agent 驱动，OS 不自动 spawn）

        workspace 由 run 的 workspace_path 决定（同一 agent 树共享一个 workspace
        与 dag.json），任意树内 run_id 都可触发。

        返回 {"ok", "sha", "affected_steps", "restored", "removed", "error"}。"""
        ri = self.runs.get(run_id)
        if not ri:
            # 尝试从 state 恢复
            ws = self._find_workspace_for_run(run_id)
            if not ws:
                return {"ok": False, "error": "run not found"}
        else:
            ws = ri.workspace_path
            if not ws:
                ws = self._find_workspace_for_run(run_id)
        if not ws:
            return {"ok": False, "error": "run has no workspace"}

        # 1) git 回退 workspace 文件到该 step commit
        co = self.recorder.checkout_step(step_id, ws)
        if not co.get("ok"):
            return {"ok": False, "error": co.get("error", "checkout failed"),
                    "sha": co.get("sha")}

        # 2) 同步 dag.json：该 step + 下游重置 pending
        affected: list[str] = []
        try:
            dag = dp.load_dag(ws)
            steps = dag.get("steps", [])
            affected = dp.get_descendants(steps, step_id)  # [step]+下游，拓扑序
            dp.reset_steps(steps, affected)
            dp.save_dag(ws, dag)
        except Exception as e:
            logger.warning(f"dag_checkout: reset dag.json failed: {e}")

        # 3) 将 reset 后的 dag.json commit 到新分支，确保分支间的 dag.json 有差异
        if affected:
            try:
                git_cwd = self.recorder._git_cwd(ws)
                subprocess.run(
                    ["git", "add", "."],
                    cwd=git_cwd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=15
                )
                r_commit = subprocess.run(
                    ["git", "commit", "-m",
                     f"[checkout:{self.recorder._ws_id(ws)}:{step_id}] reset {len(affected)} step(s) to pending"],
                    cwd=git_cwd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=15
                )
                if r_commit.returncode != 0:
                    stderr = r_commit.stderr.strip()
                    if "nothing to commit" not in stderr and "no changes" not in stderr:
                        logger.warning(f"dag_checkout: commit reset dag.json failed: {stderr[:200]}")
            except Exception as e:
                logger.warning(f"dag_checkout: commit reset dag.json exception: {e}")

        logger.info(f"dag_checkout: run={run_id[:8]} step={step_id} "
                    f"sha={co.get('sha', '')[:8]} affected={affected}")

        # 4) 从内存中清除该 workspace 的子 agent run，保留主 agent（root run）。
        #    主 agent 是回退后继续执行的入口，必须保留。
        #    子 agent（有 parent_run_id）的状态已随 git 回退失效，需要清除，
        #    避免后台节流线程把旧 runs 写回分片文件，覆盖回退后的状态。
        to_remove = [
            rid for rid, ri in self.runs.items()
            if ri.workspace_path == ws and ri.parent_run_id is not None
        ]
        for rid in to_remove:
            del self.runs[rid]
            logger.info(f"dag_checkout: removed child run {rid[:8]} from memory (workspace {self.recorder._ws_id(ws)})")

        self._mark_dirty()
        return {
            "ok": True,
            "sha": co.get("sha"),
            "branch": co.get("branch"),
            "affected_steps": affected,
            "restored": co.get("restored"),
            "removed": co.get("removed"),
            "rerun_downstream": rerun_downstream,
            "error": None,
        }

    def _find_workspace_for_run(self, run_id: str) -> str | None:
        """在 workspace 目录中查找包含该 run_id 的 workspace 路径。
        优先查 state/runs.json，再扫描 workspaces/ 目录。"""
        # 1. 从 state/runs.json 查找
        state_file = os.path.join(self.project_root, ".agent_os", "state", "runs.json")
        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                for run in state.get("runs", []):
                    if run.get("run_id") == run_id:
                        wp = run.get("workspace_path")
                        if wp and os.path.isdir(wp):
                            return wp
            except Exception:
                pass
        # 2. 扫描 state/records/ 目录
        records_dir = os.path.join(self._state_dir, "records")
        if os.path.isdir(records_dir):
            for ws_name in os.listdir(records_dir):
                if os.path.isdir(os.path.join(records_dir, ws_name, run_id)):
                    # 找到 run_id 对应的 workspace
                    ws_path = os.path.join(self.project_root, ".agent_os", "workspaces", ws_name)
                    if os.path.isdir(ws_path):
                        return ws_path
        return None

    def dag_status_by_workspace(self, workspace_id: str) -> dict:
        """通过 workspace 目录名直接查 DAG 状态。"""
        ws = os.path.join(self.project_root, ".agent_os", "workspaces", workspace_id)
        if not os.path.isdir(ws):
            return {"ok": False, "error": "workspace not found"}
        return self._dag_status_for_ws(ws)

    def _dag_status_for_ws(self, ws: str) -> dict:
        """给定 workspace 路径，返回 DAG 状态。"""
        try:
            dag = dp.load_dag(ws)
            steps = dag.get("steps", [])
            order = dp.topo_order(steps) if steps else []
            by_id = {s["id"]: s for s in steps}
            step_commits = self.recorder.list_step_commits(ws)
            commit_by_sha = {c["sha"]: c for c in step_commits}
            ordered = []
            for i in order:
                step = by_id[i]
                node = {
                    "id": step["id"],
                    "name": step.get("name", ""),
                    "status": step.get("status", "pending"),
                    "depends_on": step.get("depends_on", []),
                    "prompt": step.get("prompt", "")[:200],
                    "summary": "",
                    "files": [],
                    "sha": None,
                    "date": None,
                }
                for c in step_commits:
                    if c.get("step_id") == step["id"]:
                        node["sha"] = c.get("sha")
                        node["date"] = c.get("date")
                        node["files"] = self.recorder.commit_files(c.get("sha"), ws)
                        msg = c.get("message", "")
                        if "] " in msg:
                            node["summary"] = msg.split("] ", 1)[1][:200]
                        break
                for rid, run in self.runs.items():
                    if run.step_id == step["id"] and run.reported_result:
                        node["summary"] = run.reported_result[:200]
                        break
                ordered.append(node)
        except Exception as e:
            return {"ok": False, "error": f"load dag failed: {e}"}
        return {"ok": True, "steps": ordered, "step_commits": step_commits}

    def dag_status(self, run_id: str) -> dict:
        """返回该 run 所在 workspace 的 dag.json 状态 + step commit 列表 + agent 产出摘要。"""
        ri = self.runs.get(run_id)
        if not ri or not ri.workspace_path:
            ws = self._find_workspace_for_run(run_id)
            if not ws:
                return {"ok": False, "error": "run not found or no workspace"}
        else:
            ws = ri.workspace_path
        return self._dag_status_for_ws(ws)

    def clear_completed(self) -> int:
        """清理所有已完成的根 run（含其子树）。返回删除的根数量。"""
        roots = [ri for ri in self.runs.values()
                 if not ri.parent_run_id and ri.status in (
                     RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED)]
        count = 0
        for r in roots:
            if self.delete_run(r.run_id, recursive=True) > 0:
                count += 1
        return count

    def list_runs(self) -> list[dict]:
        """列出所有 run 的摘要。"""
        result = []
        for ri in self.runs.values():
            result.append({
                "run_id": ri.run_id,
                "prompt": ri.prompt[:100],
                "status": ri.status.value,
                "session_id": ri.session_id,
                "parent_run_id": ri.parent_run_id,
                "children_run_ids": ri.children_run_ids,
                "started_at": ri.started_at.isoformat(),
                "completed_at": ri.completed_at.isoformat() if ri.completed_at else None,
                "events": list(ri.output_events),
                "events_count": len(ri.output_events),
                "turns": len(ri.turn_markers),
            })
        return result

    def get_tree(self) -> list[dict]:
        """返回 agent 树结构（只返回根节点，children 嵌套）。"""
        roots = [ri for ri in self.runs.values() if not ri.parent_run_id]
        return [self._build_tree_node(ri) for ri in roots]

    @staticmethod
    def _unwrap_task_prompt(prompt: str, system_prompt: str = "") -> str:
        """提取真实任务文本供树视图标题与 resume_parent 子任务摘要共用。

        按优先级依次尝试：
        1. 从 system_prompt 的 ## Task 段提取（新格式：任务指令在 system prompt 中）
        2. 从 prompt 的 [Your Task]...[/Your Task] 提取（旧格式：_wrap_child_prompt 包装）
        3. 剥掉通信协议头后取首行"""
        import re as _re
        # 优先从 system_prompt 提取 ## Task 段
        if system_prompt:
            m = _re.search(r'## Task\n([\s\S]+?)(?=\n## |\Z)', system_prompt)
            if m:
                task = m.group(1).strip()
                if task:
                    return task
        # 兼容旧格式：[Your Task]...[/Your Task]
        m = _re.search(r'\[Your Task\]\n?([\s\S]*?)\n?\[/Your Task\]', prompt)
        if m:
            return m.group(1).strip()
        clean = _re.sub(r'\[Agent OS Communication Protocol[\s\S]*?\[/Agent OS Communication Protocol\]\s*', '', prompt)
        clean = _re.sub(r'\[Mandatory Closing Step\][\s\S]*?\[/Mandatory Closing Step\]', '', clean).strip()
        return clean.split('\n')[0].strip() or prompt[:80]

    def _build_tree_node(self, run_info: RunInfo) -> dict:
        """递归构建树节点。"""
        children = []
        for child_id in run_info.children_run_ids:
            child = self.runs.get(child_id)
            if child:
                children.append(self._build_tree_node(child))

        # 提取可读标题：优先从 system_prompt 的 ## Task 段提取，
        # 否则兼容旧格式 [Your Task]...[/Your Task]
        display_prompt = self._unwrap_task_prompt(
            run_info.prompt or "",
            run_info.system_prompt or "",
        )

        return {
            "run_id": run_info.run_id,
            "prompt": display_prompt[:120],
            "goal": run_info.goal or "",
            "goal_retries": run_info.goal_retries,
            "label": run_info.label,
            "status": run_info.status.value,
            "session_id": run_info.session_id,
            "started_at": run_info.started_at.isoformat(),
            "completed_at": run_info.completed_at.isoformat() if run_info.completed_at else None,
            "events": list(run_info.output_events),
            "turns": len(run_info.turn_markers),
            "interactive": run_info.interactive,
            "task_type": run_info.task_type,
            "model": run_info.model,
            "is_root": run_info.parent_run_id is None,
            "workspace_path": run_info.workspace_path,
            "children": children,
        }

    def get_run(self, run_id: str) -> RunInfo | None:
        return self.runs.get(run_id)

    async def stream_output(self, run_id: str) -> AsyncGenerator[str, None]:
        """异步生成器：yield 新结构化事件（JSON 字符串）。SSE 协议。"""
        run_info = self.runs.get(run_id)
        if not run_info:
            return

        cursor = 0
        while True:
            events = list(run_info.output_events)
            while cursor < len(events):
                yield _json.dumps(events[cursor], ensure_ascii=False)
                cursor += 1

            if run_info.status not in (RunStatus.RUNNING,):
                break

            run_info._new_output_event.clear()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, run_info._new_output_event.wait, 1.0
                )
            except asyncio.TimeoutError:
                continue

    def _build_work_context(self, run_info: RunInfo) -> str:
        """收集 agent 本轮工作产出，构建监督/评估上下文。"""
        parts = []

        if run_info.reported_result:
            parts.append(f"Final report: {run_info.reported_result}")
        elif run_info._fallback_result:
            parts.append(f"Final output: {run_info._fallback_result}")

        # text 事件（完整消息）用换行分隔，text_delta（流式片段）直接拼接
        log_lines = []
        delta_buf = []
        for e in run_info.output_events:
            kind = e.get("kind", "")
            if kind == "text_delta":
                delta_buf.append(e.get("text", ""))
            elif kind in ("text", "tool_result", "report"):
                if delta_buf:
                    log_lines.append("".join(delta_buf))
                    delta_buf = []
                log_lines.append(e.get("text", ""))
        if delta_buf:
            log_lines.append("".join(delta_buf))
        if log_lines:
            parts.append("Work log:\n" + "\n".join(log_lines))

        if run_info.messages:
            msgs = "\n".join(m.get("msg", "") for m in run_info.messages[-15:])
            if msgs.strip():
                parts.append(f"Progress messages: {msgs}")

        return "\n\n".join(parts)[:12000]

    def _spawn_supervisor(self, run_info: RunInfo) -> str:
        """为执行 agent 创建监督 agent，返回 supervisor 的 run_id。
        
        不阻塞：supervisor 作为子 agent 运行，
        通过 send.py 发反馈唤醒执行 agent。
        """
        context = self._build_work_context(run_info)
        if not context.strip():
            return ""

        task_desc = run_info.goal or run_info.prompt[:200]
        supervisor_prompt = (
            f"## 审查任务\n{task_desc}\n\n"
            f"## Agent 产出\n{context[:8000]}\n\n"
            f"## 指令\n"
            f"审查 agent 产出是否满足所有标准。\n"
            f"全部满足 → `python report.py --result \"PASS\"` 结束审查\n"
            f"有问题 → `python send.py --msg \"CORRECTION: <具体问题>\"` 告知执行 agent。"
            f"**不要调 report.py**，直接结束即可，下一轮会被自动 resume。"
        )

        sup_system_prompt = (
            f"你是严格审查 AI agent 工作的监督者。\n"
            f"验证 agent 产出是否满足以下所有标准：\n\n"
            f"{run_info.supervisor}\n\n"
            f"Be critical and thorough.\n"
            f"All criteria met → `python report.py --result \"PASS\"`\n"
            f"Issues found → `python send.py --msg \"CORRECTION: <feedback>\"` to the agent.\n"
            f"Do NOT call report.py after sending feedback. Just exit.\n"
            f"You will be resumed automatically for next review round."
        )

        sup_run_id = self.start_run(
            prompt=supervisor_prompt,
            agent_name=f"supervisor-{run_info.run_id[:6]}",
            parent_run_id=run_info.run_id,
            interactive=False,
            system_prompt=sup_system_prompt,
            model=run_info.model,
            task_type="generative",
            env_extras={"AGENT_OS_PARENT_RUN_ID": run_info.run_id},
        )
        logger.info(
            f"[{run_info.run_id[:8]}] Supervisor spawned: {sup_run_id[:8]}, "
            f"waiting for supervisor review"
        )
        return sup_run_id

    def _evaluate_goal(self, run_info: RunInfo) -> tuple[bool, str]:
        """评估 agent 是否达成了 goal。返回 (is_met, reason)。
        起一个独立的 codebuddy 子进程做语义判断。

        上下文从 4 个来源收集后统一截断到 12000 chars：
        1. reported_result / _fallback_result（agent 的最终输出）
        2. output_events 中的 text / tool_result / report（结构化事件，最精炼）
        3. output_lines（raw 对话，去冗余后兜底）
        4. messages（interactive agent 的进度消息）
        """
        goal = run_info.goal or ""
        if not goal:
            return True, "no goal"

        full_context = self._build_work_context(run_info)
        logger.debug(f"[{run_info.run_id[:8]}] _evaluate_goal: context len={len(full_context)}, "
                     f"reported={bool(run_info.reported_result)}, "
                     f"fallback={bool(run_info._fallback_result)}, "
                     f"events={len(run_info.output_events)}, "
                     f"messages={len(run_info.messages)}")
        if not full_context.strip():
            return True, "no content to evaluate (assume met)"

        return self._backend.evaluate(
            goal=goal,
            context=full_context,
            cwd=self.project_root,
        )

    MAX_GOAL_RETRIES = 5

    def set_goal(self, run_id: str, goal: str, max_retries: int | None = None) -> bool:
        """为此 run 设置 goal。可指定 max_retries（None=使用全局默认 MAX_GOAL_RETRIES）。"""
        run_info = self.runs.get(run_id)
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
        run_info = self.runs.get(run_id)
        if not run_info:
            return False
        run_info.goal_retries = getattr(run_info, '_max_goal_retries', self.MAX_GOAL_RETRIES)
        logger.info(f"[{run_id[:8]}] skip_goal: goal retries maxed out, goal evaluation disabled")
        return True

    def _on_run_completed(self, run_info: RunInfo) -> None:
        """当一个 run 完成时，检查是否需要触发 resume。
        若该 agent 有 goal 且 COMPLETED 但未达成，自动重启。"""
        # 自动补齐 step goal：有 step_id 但无 goal 时，从 dag.json 查找
        if not run_info.goal and run_info.step_id and run_info.workspace_path:
            try:
                dag = dp.load_dag(run_info.workspace_path)
                for s in dag.get("steps", []):
                    if s.get("id") == run_info.step_id:
                        goal_from_dag = s.get("goal") or ""
                        if goal_from_dag:
                            run_info.goal = goal_from_dag
                            logger.info(
                                f"[{run_info.run_id[:8]}] Goal auto-filled from dag.json "
                                f"for step {run_info.step_id}"
                            )
                        break
            except Exception as e:
                logger.debug(f"[{run_info.run_id[:8]}] Goal auto-fill failed: {e}")
        
        # === Supervisor / Goal 二选一 ===
        if run_info.supervisor and not run_info.interactive \
                and run_info.status == RunStatus.COMPLETED:
            existing_sup = getattr(run_info, '_active_supervisor', None)
            # 已有 supervisor session → resume 同一会话，继续审查
            if existing_sup and existing_sup in self.runs:
                sup_ri = self.runs[existing_sup]
                context = self._build_work_context(run_info)
                feedback = f"## Agent 新一轮产出\n\n{context[:8000]}\n\n请继续审查。满意后 report.py --result \"PASS\""
                logger.info(f"[{run_info.run_id[:8]}] Resuming supervisor {existing_sup[:8]} for next round")
                self.continue_run(existing_sup, feedback, source="os")
                object.__setattr__(run_info, '_waiting_supervisor', existing_sup)
                return
            # 首次创建 supervisor
            sup_run_id = self._spawn_supervisor(run_info)
            if sup_run_id:
                object.__setattr__(run_info, '_active_supervisor', sup_run_id)
                object.__setattr__(run_info, '_waiting_supervisor', sup_run_id)
                run_info.add_text_line("[Agent OS] Waiting for supervisor review...", kind="system")
                return
            run_info.supervisor = None

        # === Goal 评估（仅对 generative agent 生效） ===
        max_retries = getattr(run_info, '_max_goal_retries', None) or self.MAX_GOAL_RETRIES
        if (run_info.goal and not run_info.interactive
                and run_info.status == RunStatus.COMPLETED
                and run_info.goal_retries < max_retries):
            run_info.goal_retries += 1
            is_met, reason = self._evaluate_goal(run_info)
            if not is_met:
                logger.info(
                    f"[{run_info.run_id[:8]}] Goal NOT met (retry {run_info.goal_retries}/{max_retries}): {reason}"
                )
                short = reason.split("\n", 1)[1].strip() if "\n" in reason else reason
                run_info.add_text_line(
                    f"[Agent OS] Goal not met ({run_info.goal_retries}/{max_retries}): {short}",
                    kind="system",
                )
                feedback = (
                    f"Your previous attempt did NOT achieve the goal.\n"
                    f"Goal: {run_info.goal}\n"
                    f"Evaluation reason: {reason}\n\n"
                    f"Please fix the issues and try again. This is retry "
                    f"{run_info.goal_retries}/{max_retries}."
                )
                self.continue_run(run_info.run_id, feedback, source="os")
                self._mark_dirty()
                return
            else:
                if "assume met" in reason or "exception" in reason:
                    logger.info(f"[{run_info.run_id[:8]}] Goal eval: {reason}")
                else:
                    logger.info(f"[{run_info.run_id[:8]}] Goal MET: {reason}")
                    # 只取 reason 的第二行（去掉 YES/NO），避免和 agent 输出重复
                    short = reason.split("\n", 1)[1].strip() if "\n" in reason else reason
                    run_info.add_text_line(f"[Agent OS] Goal met: {short}", kind="system")
                run_info.goal_retries = max_retries
                run_info.goal = None

        # 找到所有关联的 spawn 请求
        matched_spawn = False
        for spawn_req in self.spawn_requests.values():
            if spawn_req.is_resolved:
                continue
            if run_info.run_id in spawn_req.child_run_ids:
                matched_spawn = True
                spawn_req.completed_children.add(run_info.run_id)
                self._check_spawn_resolution(spawn_req)
        if not matched_spawn:
            logger.debug(
                f"[{run_info.run_id[:8]}] _on_run_completed: not found in any active spawn request "
                f"(spawn_requests={len(self.spawn_requests)})"
            )

    def _check_spawn_resolution(self, spawn_req: SpawnRequest) -> None:
        """检查 spawn 请求是否满足 resume 条件。"""
        if spawn_req.is_resolved:
            return

        should_resume = False
        if spawn_req.wait_strategy == "all":
            all_done = all(
                self.runs.get(cid) and self.runs[cid].status in (
                    RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED
                )
                for cid in spawn_req.child_run_ids
            )
            should_resume = all_done
        elif spawn_req.wait_strategy == "any":
            should_resume = len(spawn_req.completed_children) > 0

        if should_resume:
            spawn_req.is_resolved = True
            # 用 asyncio 调度 resume（避免 threading.Thread）
            # 兼容测试环境（没有 event loop 时 fallback 到线程）
            if self._loop and self._loop.is_running():
                asyncio.ensure_future(self._resume_parent_async(spawn_req), loop=self._loop)
            else:
                threading.Thread(
                    target=self.resume_parent, args=(spawn_req,),
                    daemon=True, name=f"resume-{spawn_req.parent_run_id[:6]}"
                ).start()

    async def _resume_parent_async(self, spawn_req: SpawnRequest) -> None:
        """异步包装 resume_parent，在线程池中执行阻塞 I/O。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.resume_parent, spawn_req)

    def resume_parent(self, spawn_req: SpawnRequest) -> None:
        """根据父 agent 进程状态决定 resume 方式：
        - 父 agent 已退出（WAITING 且进程已死）→ continue_run resume
        - 父 agent 还在运行 → 把子 agent 结果写入 messages，供父 agent 通过
          spawn.py --poll 或 Dashboard 查看
        """
        parent_run_id = spawn_req.parent_run_id
        parent_session_id = spawn_req.parent_session_id
        logger.info(f"resume_parent: parent={parent_run_id[:8]}, "
                    f"spawn_session={parent_session_id[:16] if parent_session_id else 'NONE'}")

        parent = self.runs.get(parent_run_id)
        if not parent:
            logger.error(f"resume_parent: parent {parent_run_id[:8]} not found")
            return

        # 组装子 agent 结果摘要
        parts = []
        parts.append("子 agent 执行完毕，结果如下：")
        parts.append("")

        for i, child_id in enumerate(spawn_req.child_run_ids, 1):
            child = self.runs.get(child_id)
            if not child:
                parts.append(f"子任务 {i}：状态未知")
                parts.append("")
                continue

            task_desc = self._unwrap_task_prompt(
                child.prompt, child.system_prompt or "",
            )[:100]
            parts.append(f"子任务 {i}：{task_desc}")

            if child.messages:
                parts.append("过程消息：")
                for m in child.messages:
                    parts.append(f"  - {m['msg']}")

            if child.reported_result:
                status_text = ""
                if child.user_terminated:
                    status_text = "（用户手动结束）"
                parts.append(f"最终结果{status_text}：{child.reported_result}")
            elif child._fallback_result:
                status_text = ""
                if child.user_terminated:
                    status_text = "（用户手动结束）"
                parts.append(f"最终结果{status_text}：{child._fallback_result}")
            elif child.user_terminated:
                parts.append("状态：用户在 Dashboard 手动结束此子任务（无具体输出）")
            else:
                parts.append("最终结果：(未返回结果)")

            parts.append("")

        parts.append("请基于以上结果继续工作。")
        resume_prompt = "\n".join(parts)

        # 判断父 agent 进程是否还活着
        parent_alive = parent._session is not None and parent._session.poll() is None

        if parent_alive:
            # 父 agent 进程还在运行，但无法通过 stdin 注入新对话。
            # 先 terminate 父进程，再用 continue_run 恢复（让父 agent
            # 看到子 agent 的结果并继续工作）。
            logger.info(f"resume_parent: parent {parent_run_id[:8]} still running (pid={parent._session.pid}), "
                        f"terminating and using continue_run")
            try:
                parent._session.terminate()
                parent._session.wait(timeout=5)
            except Exception as e:
                logger.warning(f"resume_parent: terminate parent failed: {e}")
            # 不设 WAITING — exit handler 看到 WAITING 且子 agent 已完成
            # 会把状态改为 FAILED（terminate 导致非零退出码）。设 COMPLETED
            # 让 exit handler 跳过处理，由 continue_run 接管。
            parent.status = RunStatus.COMPLETED

        # 走 continue_run resume
        session_id = parent.session_id or parent_session_id
        if not session_id:
            logger.error(f"resume_parent: FAILED - no session_id for {parent_run_id[:8]}")
            parent.add_text_line("[Agent OS] Error: Cannot resume - no session_id available", kind="error")
            parent.status = RunStatus.FAILED
            parent.completed_at = datetime.now()
            return

        parent.session_id = session_id
        logger.info(f"resume_parent: parent {parent_run_id[:8]} calling continue_run")
        ok = self.continue_run(parent_run_id, resume_prompt, source="os")
        if not ok:
            logger.error(f"resume_parent: continue_run returned False for {parent_run_id[:8]}")
            parent.add_text_line("[Agent OS] Error: continue_run failed", kind="error")
            parent.status = RunStatus.FAILED
            parent.completed_at = datetime.now()

    def _build_env(self, run_id: str, workspace_name: str | None = None,
                    env_extras: dict | None = None) -> dict:
        """构建环境变量，让子 agent 知道自己的位置。

        根 agent（AGENT_OS_WORKSPACE 未在当前环境中）自动创建工作目录并注入；
        子 agent 通过进程继承链自动获得父 agent 的工作目录，无需额外处理。

        workspace_name: 仅对根 agent 生效。传入时使用命名目录
            ``workspaces/<sanitized_name>``（已存在则复用，实现按名字跨 run 持久化）；
            不传则沿用 ``workspaces/<run_id>``。

        env_extras: 如果包含 AGENT_OS_WORKSPACE，则跳过创建新 workspace（子 agent 继承父的）。
        """
        import os
        env = os.environ.copy()
        env.update({
            "AGENT_OS_RUN_ID": run_id,
            "AGENT_OS_PORT": str(self.port),
        })
        # 如果 env_extras 指定了 workspace，直接使用（子 agent 继承父 agent 的）
        if env_extras and "AGENT_OS_WORKSPACE" in env_extras:
            env["AGENT_OS_WORKSPACE"] = env_extras["AGENT_OS_WORKSPACE"]
            logger.info(f"[{run_id[:8]}] Inherited workspace from parent: {env_extras['AGENT_OS_WORKSPACE']}")
        # 工作目录：子 agent 继承父 agent 的；根 agent 新建
        elif "AGENT_OS_WORKSPACE" not in env:
            dir_name = sanitize_workspace_name(workspace_name) if workspace_name else run_id
            # 上溯到 .agent_os/ 根目录，与 dag.py router 的 start_dag 保持一致
            _aos_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            workspace_path = os.path.join(_aos_root, "workspaces", dir_name)
            reused = os.path.isdir(workspace_path)
            os.makedirs(workspace_path, exist_ok=True)
            env["AGENT_OS_WORKSPACE"] = workspace_path
            action = "Reused" if reused else "Created"
            logger.info(f"[{run_id[:8]}] {action} workspace: {workspace_path}")
        # 透传其他 env_extras（如 AGENT_OS_STEP_ID）
        if env_extras:
            for k, v in env_extras.items():
                if k != "AGENT_OS_WORKSPACE":  # 上面已处理
                    env[k] = v
        return env

    def _get_task_hook_config(self) -> str | None:
        """返回 Task Hook 的 JSON 配置文件路径，按需生成。

        通过 PreToolUse hook 拦截 CodeBuddy 原生 Task 工具，
        转发到 OS spawn 逻辑，替代 MCP。
        """
        import json as _json
        config_dir = os.path.join(self._state_dir, "hooks")
        config_file = os.path.join(config_dir, "task_hook_config.json")
        os.makedirs(config_dir, exist_ok=True)

        hook_script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "hooks", "task_hook.py"
        )
        hook_script = os.path.abspath(hook_script)

        python_exe = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", ".venv", "Scripts", "python.exe"
        )
        python_exe = os.path.abspath(python_exe)
        if not os.path.exists(python_exe):
            python_exe = sys.executable

        config = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Task",
                        "hooks": [{
                            "type": "command",
                            "command": f"{python_exe} {hook_script}",
                        }]
                    }
                ]
            }
        }
        with open(config_file, "w", encoding="utf-8") as f:
            _json.dump(config, f, indent=2)
        return config_file

    @staticmethod
    def _build_root_system_prompt(workspace_path: str = "") -> str:
        """根 agent 的 Agent OS system prompt。"""
        ws = workspace_path or ".agent_os/workspaces/<run_id>/"
        return (
            "You are running under Agent OS, a multi-agent orchestration system.\n\n"
            "## Workspace\n\n"
            f"Your workspace is at {ws}\n"
            "This is the persistent file memory for the entire task.\n"
            "The env var $AGENT_OS_WORKSPACE points to this directory.\n"
            "**All file read/write MUST use the workspace directory.** "
            "Read files with `cat $AGENT_OS_WORKSPACE/...` or use the Read tool on workspace paths.\n"
            "dag.json is at $AGENT_OS_WORKSPACE/dag.json (NOT in the current cwd).\n"
            "Use `python .agent_os/dag.py --ready` which reads from $AGENT_OS_WORKSPACE automatically.\n\n"
            "## Agent Types\n\n"
            "- generative: runs autonomously, calls report.py when done\n"
            "- interactive: waits for user to click Done in the dashboard\n"
            "- explore: cannot spawn children, for exploration tasks\n\n"
            "## Available Tools\n\n"
            "- Create sub-agents: use the Task tool (subagent_type=generative|interactive) "
            "to spawn child agents. Sub-agents share your workspace.\n"
            "- report.py: `python .agent_os/report.py --result \"<summary>\"`\n"
            "  Mark your task as complete. Your parent agent will be resumed.\n"
            "- send.py: `python .agent_os/send.py --msg \"<message>\"`\n"
            "  Send progress updates to your parent agent.\n"
        )

    @staticmethod
    def _build_subagent_system_prompt(task_type: str = "generative",
                                       task_prompt: str = "",
                                       workspace_path: str | None = None) -> str:
        """生成注入到子 agent 的 system prompt。"""
        ws_rel = ".agent_os/workspaces/<任务名>/"
        if workspace_path:
            import os as _os
            ws_name = _os.path.basename(workspace_path.rstrip("/\\"))
            ws_rel = f".agent_os/workspaces/{ws_name}"

        task_prompt = task_prompt.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

        if task_type == "interactive":
            completion = (
                "## How to Complete\n\n"
                "You are an **interactive** agent. Your task requires user input or confirmation.\n"
                "When ready for user review, inform the user what you've done and what input "
                "you need. The user will click **Done** in the dashboard to mark your task as "
                "complete.\n"
                "- ⚠️ Do NOT call report.py — it will be ignored. Only the Done button completes you.\n"
                "- If the user has already provided input, you may still need to wait for Done.\n\n"
            )
        else:
            completion = (
                "## How to Complete\n\n"
                "You are a **generative** agent. You work autonomously and decide when to finish.\n"
                "When your task is complete, you **must** call `python report.py "
                "--result \"<summary>\"` to report your results. Without this, your task will be "
                "marked as **failed** even if the work is done.\n"
                "- ⚠️ report.py is MANDATORY for completion. The process exiting alone is not enough.\n"
                "- The user can also click **Done** to manually complete you at any time.\n\n"
            )

        base = (
            "You are a sub-agent running under Agent OS.\n\n"
            "## Workspace\n\n"
            f"Your shared workspace is at: {ws_rel}\n"
            "This is the persistent file memory for the entire task — "
            "all agents in this pipeline read and write to this same directory. "
            "Files you create here will be accessible to downstream agents.\n"
            "**All file read/write MUST use the workspace directory.** "
            "Use `cat $AGENT_OS_WORKSPACE/...` or Read/Write tools with the full workspace path.\n\n"
            + completion +
            "## Available Tools\n\n"
            "- Create sub-agents: use the Task tool (subagent_type=generative|interactive) "
            "for further parallel work.\n"
            "- report.py: `python report.py --result \"<summary>\"`\n"
            "  Call this when your task is done. Your parent agent will be resumed.\n"
            "- send.py: `python send.py --msg \"<message>\"`\n"
            "  Send progress updates to your parent agent."
        )
        if task_prompt:
            base += f"\n## Task\n{task_prompt}\n"
        return base

    def _start_reader(self, run_info: RunInfo) -> None:
        """启动后台读取线程。"""
        reader = threading.Thread(
            target=self._read_output,
            args=(run_info,),
            daemon=True,
            name=f"reader-{run_info.run_id[:6]}",
        )
        run_info._reader_thread = reader
        reader.start()

    def _read_output(self, run_info: RunInfo) -> None:
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
                    run_info.status = RunStatus.PLAN_PENDING
                    plan_file = self._find_latest_plan_file()
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

            if run_info.status == RunStatus.PLAN_PENDING:
                logger.info(f"[{run_info.run_id[:8]}] Session ended in plan_pending — waiting for user approval")
                self._mark_dirty()
            elif run_info.status == RunStatus.RUNNING:
                if run_info.interactive:
                    # interactive: 只有用户点 Done 才算完成，进程退出继续等
                    logger.debug(f"[{run_info.run_id[:8]}] Interactive - staying running")
                elif run_info.reported_result:
                    # generative: 已通过 report.py 汇报结果，正常结束
                    run_info.status = (
                        RunStatus.COMPLETED if (session.returncode or 0) == 0
                        else RunStatus.FAILED
                    )
                    run_info.completed_at = datetime.now()

                    # DAG 状态更新
                    if run_info.step_id and run_info.workspace_path:
                        try:
                            dag = dp.load_dag(run_info.workspace_path)
                            steps = dag.get("steps", [])
                            if dp.mark_done(steps, run_info.step_id):
                                dp.save_dag(run_info.workspace_path, dag)
                                logger.info(f"[{run_info.run_id[:8]}] DAG step marked done: {run_info.step_id}")
                        except Exception as e:
                            logger.warning(f"[{run_info.run_id[:8]}] DAG mark_done failed: {e}")

                    turn_num = len(run_info.turn_markers)
                    if run_info.workspace_path:
                        try:
                            self.recorder.turn_done(run_info.run_id, turn_num, run_info.workspace_path)
                        except Exception:
                            pass

                    logger.info(f"[{run_info.run_id[:8]}] Marked {run_info.status.value}, calling _on_run_completed")
                    self._on_run_completed(run_info)

                    if run_info.workspace_path and not run_info._recorded:
                        run_info._recorded = True
                        try:
                            final = run_info.reported_result or "(无输出)"
                            self.recorder.run_done(
                                run_id=run_info.run_id, result=final,
                                workspace_path=run_info.workspace_path,
                            )
                        except Exception:
                            pass
                elif run_info.parent_run_id:
                    # 检查是否是活跃 supervisor（通过 session 持久化，reported_result 可能不设）
                    parent_ri = self.runs.get(run_info.parent_run_id)
                    is_active_sup = parent_ri and getattr(parent_ri, '_active_supervisor', None) == run_info.run_id
                    sup_done = getattr(run_info, '_supervisor_done', False)
                    if is_active_sup or sup_done:
                        if sup_done:
                            logger.info(f"[{run_info.run_id[:8]}] Supervisor PASS complete")
                            run_info.status = RunStatus.COMPLETED
                        else:
                            logger.info(
                                f"[{run_info.run_id[:8]}] Supervisor exited without report.py, "
                                f"will be resumed next round"
                            )
                        # 不标状态，run_info 保留在 runs 中供下次 continue_run
                    else:
                        # DAG step 子 agent：必须通过 report.py 完成
                        logger.warning(
                            f"[{run_info.run_id[:8]}] Agent exited without calling report.py "
                            f"(code={session.returncode}), marking failed"
                        )
                        run_info.add_text_line(
                            "[Agent OS] Agent process exited without calling report.py — step failed",
                            kind="error",
                        )
                        run_info.status = RunStatus.FAILED
                    run_info.completed_at = datetime.now()
                    self._on_run_completed(run_info)
                else:
                    # 根 agent（调度 agent / 用户会话）：进程退出即完成
                    logger.info(
                        f"[{run_info.run_id[:8]}] Root agent exited "
                        f"(code={session.returncode}), auto-completing"
                    )
                    run_info.status = (
                        RunStatus.COMPLETED if (session.returncode or 0) == 0
                        else RunStatus.FAILED
                    )
                    run_info.completed_at = datetime.now()
                    logger.info(f"[{run_info.run_id[:8]}] Marked {run_info.status.value}")
                    self._on_run_completed(run_info)
            elif run_info.status == RunStatus.WAITING:
                # supervisor 审查中：不要改状态，等待 send.py 发 CORRECTION 或 report.py 发 PASS
                waiting_sup = getattr(run_info, '_waiting_supervisor', None)
                if waiting_sup and waiting_sup in self.runs:
                    logger.info(f"[{run_info.run_id[:8]}] Waiting for supervisor review, keep WAITING")
                else:
                    all_children_done = True
                    for spawn_req in self.spawn_requests.values():
                        if spawn_req.parent_run_id == run_info.run_id and not spawn_req.is_resolved:
                            all_children_done = False
                            break
                    if all_children_done:
                        run_info.status = (
                            RunStatus.COMPLETED if (session.returncode or 0) == 0
                            else RunStatus.FAILED
                        )
                        run_info.completed_at = datetime.now()
                        logger.info(f"[{run_info.run_id[:8]}] WAITING agent done, marked {run_info.status.value}")
                        if run_info.workspace_path and not run_info._recorded:
                            run_info._recorded = True
                            try:
                                final = run_info.reported_result or run_info._fallback_result or "(无输出)"
                                self.recorder.run_done(
                                    run_id=run_info.run_id, result=final,
                                    workspace_path=run_info.workspace_path,
                                )
                            except Exception:
                                pass
                    else:
                        logger.info(f"[{run_info.run_id[:8]}] WAITING agent ended, children still running")
            else:
                logger.debug(f"[{run_info.run_id[:8]}] Status already {run_info.status.value}, skip exit handler")

        except Exception as e:
            run_info.add_text_line(f"[ERROR] {e}", kind="error")
            run_info.status = RunStatus.FAILED
            run_info.completed_at = datetime.now()
            self._on_run_completed(run_info)

            if run_info.workspace_path and not run_info._recorded:
                run_info._recorded = True
                try:
                    self.recorder.run_done(
                        run_id=run_info.run_id,
                        result=f"异常: {str(e)[:200]}",
                        workspace_path=run_info.workspace_path,
                    )
                except Exception:
                    pass

        finally:
            if run_info._loop and run_info._new_output_event:
                run_info._new_output_event.set()
