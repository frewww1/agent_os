"""Claude CLI 子进程管理器 — 启动、流式输出、会话管理、父子关系、自动 resume。"""
import asyncio
import json as _json
import logging
import os
import platform
import subprocess
import tempfile
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncGenerator

import dag_planner as dp
from recorder import Recorder


USE_SHELL = False  # 不用 shell=True，避免特殊字符被 CMD 解析破坏参数

# 模型列表缓存文件（由 start.ps1 在有 TTY 的终端里解析 `<cli> --help` 写入）
MODELS_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state", "models.json"
)

# 日志配置
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
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


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    WAITING = "waiting"  # 主 agent 等待子 agent 完成


@dataclass
class RunInfo:
    """单次 claude 执行的状态信息。"""
    run_id: str
    prompt: str
    status: RunStatus = RunStatus.RUNNING
    interactive: bool = False
    session_id: str | None = None
    model: str | None = None                   # CLI --model（None = CLI 默认）
    task_type: str = "generative"              # generative | interactive（仅子 agent）
    reported_result: str | None = None         # 最终结果（调 report.py 设置）
    user_terminated: bool = False              # 用户在 Dashboard 点 Done 强制结束
    messages: list = field(default_factory=list)  # 中间消息列表 [{"time":..., "msg":...}]
    _fallback_result: str | None = field(default=None, repr=False)  # 从 stream-json result 事件捕获
    parent_run_id: str | None = None       # 父 agent 的 run_id
    children_run_ids: list = field(default_factory=list)  # 子 agent 的 run_id 列表
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    exit_code: int | None = None
    workspace_path: str | None = None        # 该 run 的工作目录（根 agent 自建，子 agent 继承）
    step_id: str | None = None                # DAG step 标识；有值则 report 时打 [step:<id>] commit
    system_prompt: str | None = None          # 启动时的 system prompt（用于 /clear 后恢复）
    _recorded: bool = False                   # 防止重复写入 record.json
    output_lines: deque = field(default_factory=lambda: deque(maxlen=10000))
    # 结构化事件流，前端按 kind 渲染（与 output_lines 并行维护，不破坏旧接口）
    # 每个事件: {"seq": int, "ts": iso, "kind": str, "turn": int, ...payload}
    output_events: deque = field(default_factory=lambda: deque(maxlen=10000))
    _event_seq: int = 0
    turn_markers: list = field(default_factory=list)
    # Spawn/resume 相关
    spawn_id: str | None = None            # 关联的 spawn 请求 ID
    label: str | None = None              # 用户自定义显示名称
    _process: subprocess.Popen | None = field(default=None, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _new_output_event: asyncio.Event | None = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    def add_event(self, kind: str, **payload) -> dict:
        """追加结构化事件（前端按 kind 渲染）。同时唤醒 SSE 流。"""
        self._event_seq += 1
        event = {
            "seq": self._event_seq,
            "ts": datetime.now().isoformat(),
            "kind": kind,
            "turn": max(1, len(self.turn_markers)),
        }
        event.update(payload)
        self.output_events.append(event)
        # 唤醒 SSE 监听者
        if self._loop and self._new_output_event:
            try:
                self._loop.call_soon_threadsafe(self._new_output_event.set)
            except RuntimeError:
                pass  # loop 已关闭
        # 标记需要持久化
        _pm = globals().get("_persist_pm_ref")
        if _pm is not None:
            try:
                _pm._mark_dirty()
            except Exception:
                pass
        return event

    def add_text_line(self, line: str, kind: str = "system") -> None:
        """兼容写法：同时追加到 output_lines（旧 API）和 output_events。"""
        self.output_lines.append(line)
        self.add_event(kind, text=line)


@dataclass
class SpawnRequest:
    """一次 spawn 请求，追踪多个子 agent 的完成状态。"""
    spawn_id: str
    parent_run_id: str
    parent_session_id: str
    child_run_ids: list
    wait_strategy: str  # "all" or "any"
    completed_children: set = field(default_factory=set)
    is_resolved: bool = False


class ProcessManager:
    """管理多个 claude CLI 子进程，支持父子嵌套和自动 resume。"""

    def __init__(self, project_root: str = ".", cli_command: str = "claude", port: int = 8420,
                 default_model: str | None = None):
        self.project_root = project_root
        self.port = port
        self.default_model = default_model
        # 解析完整路径（Windows 上 .cmd 文件需要完整路径才能不用 shell=True）
        import shutil
        resolved = shutil.which(cli_command)
        self.cli_command = resolved if resolved else cli_command

        # Windows 修复：.CMD shim 用 `%*` 转发参数，对含换行 `\n` 的 prompt 会
        # 在换行处截断（CMD 把后续内容当作新命令），导致 --output-format stream-json
        # 等参数丢失，claude CLI 回退到纯文本输出。
        # 解决方案：解析 .CMD 找到底层 .js，改为直接 node 调用。
        self.cli_prefix = self._resolve_node_cli(self.cli_command)
        logger.info(f"ProcessManager: cli={self.cli_command}")
        if len(self.cli_prefix) > 1:
            logger.info(f"ProcessManager: bypassing .CMD shim -> node {self.cli_prefix[1]}")
        self.runs: dict[str, RunInfo] = {}
        self.spawn_requests: dict[str, SpawnRequest] = {}
        self._resume_callback = None  # set by main.py for async resume
        self._models_cache: list[str] | None = None  # `<cli> --help` 解析出的模型列表缓存
        self.recorder = Recorder(project_root=self.project_root)  # workspace 记忆层

        # 持久化文件
        self._state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
        os.makedirs(self._state_dir, exist_ok=True)
        self._runs_file = os.path.join(self._state_dir, "runs.json")
        # 启动时尝试恢复历史 runs（仅元数据 + 事件流；进程引用全部丢失，
        # 这些 run 之后都是只读的，能查看 / 删除 / 导出但不能 resume）
        self._load_runs_from_disk()
        # 节流写盘：每次 add_event 不直接落盘，由后台线程定期落盘
        self._save_lock = threading.Lock()
        self._save_dirty = False
        self._save_thread = threading.Thread(
            target=self._periodic_save_worker, daemon=True, name="agent-os-save"
        )
        self._save_thread.start()
        # 暴露给 RunInfo.add_event() 触发 dirty 标记（避免循环依赖）
        globals()["_persist_pm_ref"] = self

        # 子 agent 超时看护：默认 20 分钟无新事件视为 stale
        self._idle_timeout_sec = 20 * 60
        self._timeout_thread = threading.Thread(
            target=self._timeout_watcher, daemon=True, name="agent-os-timeout"
        )
        self._timeout_thread.start()

    def _timeout_watcher(self):
        """每 30s 扫一次所有 RUNNING 的子 agent，超过 idle_timeout 强制结束。"""
        import time
        while True:
            time.sleep(30.0)
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
        """每 3 秒检查一次脏标记，dirty 时写盘。"""
        import time
        while True:
            time.sleep(3.0)
            try:
                with self._save_lock:
                    if not self._save_dirty:
                        continue
                    self._save_dirty = False
                self._save_runs_to_disk()
            except Exception as e:
                logger.warning(f"persist worker error: {e}")

    def _mark_dirty(self):
        with self._save_lock:
            self._save_dirty = True

    @staticmethod
    def _sanitize(obj):
        """递归清除字符串中的 surrogate 字符，避免 JSON 序列化失败。"""
        if isinstance(obj, str):
            # json.dumps 对 surrogate 字符 (\uD800-\uDFFF) 会直接报错，
            # 必须在进入 json 序列化前清除。
            # encode surrogatepass -> decode replace 是最可靠的方式
            return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
        if isinstance(obj, dict):
            return {k: ProcessManager._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [ProcessManager._sanitize(v) for v in obj]
        return obj

    def _serialize_run(self, ri: "RunInfo") -> dict:
        """把 RunInfo 转成可 JSON 化的字典（剥离不可序列化字段）。"""
        raw = {
            "run_id": ri.run_id,
            "prompt": ri.prompt,
            "status": ri.status.value,
            "interactive": ri.interactive,
            "session_id": ri.session_id,
            "model": ri.model,
            "task_type": ri.task_type,
            "reported_result": ri.reported_result,
            "user_terminated": ri.user_terminated,
            "workspace_path": ri.workspace_path,
            "messages": list(ri.messages),
            "fallback_result": ri._fallback_result,
            "parent_run_id": ri.parent_run_id,
            "children_run_ids": list(ri.children_run_ids),
            "started_at": ri.started_at.isoformat(),
            "completed_at": ri.completed_at.isoformat() if ri.completed_at else None,
            "exit_code": ri.exit_code,
            "output_lines": list(ri.output_lines),
            "output_events": list(ri.output_events),
            "event_seq": ri._event_seq,
            "turn_markers": list(ri.turn_markers),
            "spawn_id": ri.spawn_id,
            "label": ri.label,
            "step_id": ri.step_id,
            "system_prompt": ri.system_prompt,
        }
        return self._sanitize(raw)

    def _save_runs_to_disk(self):
        """保存所有 runs 到磁盘。

        按 workspace 分片存储：每个 workspace 写到
        ``workspaces/<ws>/state/runs.json``，让回退时 run 状态也跟着还原。
        同时写全局聚合文件 ``state/runs.json`` 供跨 workspace 查询。
        """
        try:
            # 按 workspace 分组
            by_ws: dict[str, list] = {}  # ws_path -> [run_dict]
            all_runs: list = []
            for ri in self.runs.values():
                s = self._serialize_run(ri)
                s = self._sanitize(s)
                all_runs.append(s)
                ws = ri.workspace_path or "_global"
                by_ws.setdefault(ws, []).append(s)

            # 1) 每个 workspace 写分片文件
            for ws_path, runs_list in by_ws.items():
                if ws_path == "_global":
                    continue  # 无 workspace 的 run 只进全局文件
                ws_state_dir = os.path.join(ws_path, "state")
                try:
                    os.makedirs(ws_state_dir, exist_ok=True)
                except OSError:
                    logger.warning(
                        f"save_runs: cannot access workspace {ws_path}, "
                        f"skipping per-ws save (runs still saved to global file)"
                    )
                    continue
                ws_runs_file = os.path.join(ws_state_dir, "runs.json")
                data = {
                    "schema_version": 1,
                    "saved_at": datetime.now().isoformat(),
                    "runs": runs_list,
                }
                text = _json.dumps(data, ensure_ascii=False)
                tmp = ws_runs_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, ws_runs_file)

            # 2) 全局聚合文件（兼容 /api/runs 等跨 workspace 查询）
            data = {
                "schema_version": 1,
                "saved_at": datetime.now().isoformat(),
                "runs": all_runs,
            }
            text = _json.dumps(data, ensure_ascii=False)
            tmp = self._runs_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self._runs_file)
        except Exception as e:
            logger.error(f"save_runs failed: {e}")

    def _load_runs_from_disk(self):
        """启动时从磁盘恢复历史 runs（仅作只读历史展示）。

        优先从各 workspace 的分片文件加载（workspaces/<ws>/state/runs.json），
        兜底用全局 state/runs.json。去重：同一 run_id 只保留一份。
        """
        # 收集所有候选文件：分片优先，全局兜底
        candidates: list[tuple[str, str]] = []  # [(file_path, source_tag), ...]
        workspaces_dir = os.path.join(self.project_root, ".agent_os", "workspaces")
        if os.path.isdir(workspaces_dir):
            for ws_name in os.listdir(workspaces_dir):
                ws_state = os.path.join(workspaces_dir, ws_name, "state", "runs.json")
                if os.path.exists(ws_state):
                    candidates.append((ws_state, "shard"))
        if os.path.exists(self._runs_file):
            candidates.append((self._runs_file, "global"))

        if not candidates:
            return

        seen_run_ids: set[str] = set()
        total = 0
        from collections import deque
        for file_path, source in candidates:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                data = self._sanitize(data)
                for r in data.get("runs", []):
                    rid = r.get("run_id")
                    if not rid or rid in seen_run_ids:
                        continue
                    seen_run_ids.add(rid)
                    status_str = r.get("status", "completed")
                    if status_str in ("running", "waiting"):
                        status_str = "stopped"
                    ri = RunInfo(
                        run_id=rid,
                        prompt=r.get("prompt", ""),
                        status=RunStatus(status_str),
                        interactive=r.get("interactive", False),
                        session_id=r.get("session_id"),
                        model=r.get("model"),
                        task_type=r.get("task_type", "generative"),
                        reported_result=r.get("reported_result"),
                        user_terminated=r.get("user_terminated", False),
                        messages=r.get("messages", []),
                        parent_run_id=r.get("parent_run_id"),
                        children_run_ids=r.get("children_run_ids", []),
                        started_at=datetime.fromisoformat(r["started_at"]),
                        completed_at=datetime.fromisoformat(r["completed_at"]) if r.get("completed_at") else None,
                        exit_code=r.get("exit_code"),
                        workspace_path=r.get("workspace_path"),
                        spawn_id=r.get("spawn_id"),
                        step_id=r.get("step_id"),
                        system_prompt=r.get("system_prompt"),
                    )
                    ri._fallback_result = r.get("fallback_result")
                    ri._event_seq = r.get("event_seq", 0)
                    for line in r.get("output_lines", []):
                        ri.output_lines.append(line)
                    for ev in r.get("output_events", []):
                        ri.output_events.append(ev)
                    ri.turn_markers = r.get("turn_markers", [])
                    self.runs[ri.run_id] = ri
                    total += 1
            except Exception as e:
                logger.warning(f"load_runs from {file_path} failed: {e}")

        logger.info(f"loaded {total} historical runs ({len(seen_run_ids)} unique) from {len(candidates)} files")
        self._restore_spawn_requests()

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
            spawn_id = parent.spawn_id if parent else ""
            if not spawn_id:
                # 父 agent 没有 spawn_id（可能是旧数据），生成一个
                spawn_id = f"restored_{parent_id[:8]}"
                if parent:
                    parent.spawn_id = spawn_id
            
            parent_session = parent.session_id if parent else ""
            
            # 统计已完成/失败的子 run
            completed = set()
            for cid in child_ids:
                child = self.runs.get(cid)
                if child and child.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED):
                    completed.add(cid)
            
            spawn_req = SpawnRequest(
                spawn_id=spawn_id,
                parent_run_id=parent_id,
                parent_session_id=parent_session,
                child_run_ids=child_ids,
                wait_strategy="all",
            )
            spawn_req.completed_children = completed
            # 如果所有子 run 都已完成，标记为已解决
            if len(completed) == len(child_ids):
                spawn_req.is_resolved = True
            self.spawn_requests[spawn_id] = spawn_req
            
        if parent_groups:
            logger.info(f"restored {len(parent_groups)} spawn requests from historical runs")

    @staticmethod
    def _resolve_node_cli(cli_path: str) -> list[str]:
        """如果 cli_path 是 Windows .CMD 文件包装的 node 脚本，解析出底层 .js / bin
        并返回 ['node', target]，绕过 .CMD shim 对换行 prompt 的截断 bug。
        否则返回 [cli_path]。

        支持两种 shim 格式：
          - npm 经典:   "%_prog%" "%dp0%\\...\\xxx.js" %*       (claude-internal)
          - npm 新式:   "%_prog%"  "%dp0%\\...\\bin\\xxx" %*    (codebuddy)
        """
        if not cli_path.lower().endswith('.cmd'):
            return [cli_path]
        try:
            with open(cli_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            import re
            # 通用模式：找 "%_prog%" 后面跟着的 "%dp0%..." 路径（不限定后缀）
            m = re.search(r'"%_prog%"\s+"(%dp0%[^"]+)"', content)
            if not m:
                return [cli_path]
            target_rel = m.group(1)
            dp0 = os.path.dirname(cli_path) + os.sep
            target_abs = target_rel.replace('%dp0%', dp0)
            # 规范化多余的反斜杠
            target_abs = os.path.normpath(target_abs)
            if not os.path.exists(target_abs):
                return [cli_path]
            # 校验 Node.js 能否实际加载此模块（nvm/fnm symlink 场景下文件存在但不可加载）
            try:
                import subprocess
                # 用 require.resolve 测试模块是否可解析
                check = subprocess.run(
                    ['node', '-e', f'require.resolve("{target_abs.replace(chr(92), chr(92)+chr(92))}")'],
                    capture_output=True, text=True, timeout=5,
                    cwd=os.path.dirname(target_abs),
                )
                if check.returncode != 0:
                    logger.info(
                        f"_resolve_node_cli: bypass failed for {target_abs} "
                        f"(node can't load it), using .cmd directly"
                    )
                    return [cli_path]
            except Exception as e:
                logger.info(f"_resolve_node_cli: require.resolve check failed: {e}, using .cmd directly")
                return [cli_path]
            return ['node', target_abs]
        except Exception as e:
            logger.warning(f"_resolve_node_cli failed: {e}")
        return [cli_path]

    def list_models(self, refresh: bool = False) -> list[str]:
        """返回当前 CLI 支持的模型 ID 列表，带内存缓存。

        数据来源优先级：
        1. 缓存文件 ``state/models.json`` —— 由 ``start.ps1`` 在启动 dashboard 前
           （用户终端有 TTY，``codebuddy --help`` 可秒回）解析并写入。
        2. 兜底直接运行 ``<cli> --help`` 解析 —— 仅在**有 TTY** 的环境可成功；
           dashboard 作为后台服务进程通常没有 TTY，该 CLI 的 ``--help`` 会一直等待
           TTY stdin 而挂起，因此后台进程下此兜底基本拿不到结果（靠缓存文件）。

        解析失败时返回空列表（前端据此回退到仅 Default）。
        """
        if self._models_cache is not None and not refresh:
            return self._models_cache
        # 1) 优先读缓存文件
        models = self._read_models_cache_file()
        # 2) 缓存为空时兜底跑 CLI（有 TTY 时可成功），成功则回写缓存文件
        if not models:
            models = self._parse_models_from_cli()
            if models:
                self._write_models_cache_file(models)
        self._models_cache = models
        return models

    def _read_models_cache_file(self) -> list[str]:
        """从 ``state/models.json`` 读取模型列表，文件不存在/损坏时返回空列表。"""
        try:
            if os.path.exists(MODELS_CACHE_FILE):
                # 用 utf-8-sig 读取，兼容 PowerShell `Set-Content -Encoding UTF8`
                # 写入的带 BOM 文件（普通 utf-8 解析 BOM 会报错）。
                with open(MODELS_CACHE_FILE, encoding="utf-8-sig") as f:
                    data = _json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data if x]
        except Exception as e:
            logger.warning(f"read models cache failed: {e}")
        return []

    def _write_models_cache_file(self, models: list[str]) -> None:
        """把模型列表写入 ``state/models.json``。"""
        try:
            os.makedirs(os.path.dirname(MODELS_CACHE_FILE), exist_ok=True)
            with open(MODELS_CACHE_FILE, "w", encoding="utf-8") as f:
                _json.dump(models, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"write models cache failed: {e}")

    def _parse_models_from_cli(self) -> list[str]:
        """运行 ``<cli> --help`` 解析模型列表（需 TTY，后台进程可能超时）。"""
        import re
        models: list[str] = []
        try:
            cmd = list(self.cli_prefix) + ["--help"]
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=15, shell=USE_SHELL, stdin=subprocess.DEVNULL,
            )
            text = (proc.stdout or "") + "\n" + (proc.stderr or "")
            # help 文本可能因终端宽度换行，先把括号内容拼接起来再切分
            m = re.search(r"Currently supported:\s*\(([^)]+)\)", text, re.DOTALL)
            if m:
                raw = re.sub(r"\s+", "", m.group(1))  # 去掉换行/空格
                models = [s for s in raw.split(",") if s]
            if not models:
                logger.warning("_parse_models_from_cli: 未能从 --help 解析出模型列表（可能因无 TTY 而超时）")
        except Exception as e:
            logger.warning(f"_parse_models_from_cli failed: {e}")
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
                    f"prompt={prompt[:50]}, interactive={interactive}, model={model}")


        cmd = self._build_cmd(prompt, agent_name=agent_name, system_prompt=system_prompt,
                              session_id=session_id, model=model)

        # 构建环境变量（让子 agent 知道自己的 run_id，以便嵌套 spawn）
        env = self._build_env(run_id, workspace_name=workspace_name, env_extras=env_extras)
        if env_extras:
            env.update(env_extras)
        # DEBUG: 确认 step_id 是否传递
        step_id_from_env = env.get("AGENT_OS_STEP_ID")
        if step_id_from_env:
            logger.info(f"[{run_id[:8]}] STEP_ID from env: {step_id_from_env}")

        logger.debug(f"[{run_id[:8]}] CMD: {cmd[:4]}... (prompt len={len(prompt)}, "
                     f"system_prompt len={len(system_prompt) if system_prompt else 0}, "
                     f"args after -p: {[a for a in cmd if a.startswith('--')]})")
        # cwd 使用项目根目录，让 agent 能正常加载 skill。
        # workspace 通过 AGENT_OS_WORKSPACE 环境变量告知 agent。
        workspace_cwd = self.project_root
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=workspace_cwd,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            shell=USE_SHELL,
            env=env,
        )

        loop = asyncio.get_event_loop()
        # 计算深度：根 agent depth=0，子 agent 继承父 depth+1
        _depth = 0
        if parent_run_id and parent_run_id in self.runs:
            _depth = (getattr(self.runs[parent_run_id], '_depth', 0) or 0) + 1

        run_info = RunInfo(
            run_id=run_id,
            prompt=prompt,
            session_id=session_id,  # 由 OS 预先指定，不再等 stream-json 提取
            parent_run_id=parent_run_id,
            interactive=interactive,
            model=model,
            task_type=task_type,
            workspace_path=env.get("AGENT_OS_WORKSPACE"),
            step_id=env.get("AGENT_OS_STEP_ID"),  # DAG step：由 spawn 经 env_extras 注入
            system_prompt=system_prompt,  # 保存用于 /clear 后恢复
            _process=process,
            _new_output_event=asyncio.Event(),
            _loop=loop,
        )
        run_info._depth = _depth  # 记录深度
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
            task_type = task.get("type") or task.get("agent_type") or "generative"
            child_model = task.get("model") or parent_model
            step_id = task.get("step_id")  # DAG step 标识，透传给子 agent env
            if not prompt:
                logger.warning(f"spawn_children: task {task.get('id','?')} has no prompt, skipping")
                continue
            if step_id:
                spawned_step_ids.append(step_id)

            # 为子 agent 注入 system prompt：
            # - generative: 引导 agent 自行调用 report.py 结束（参考 sgr-spawn-agent）
            # - interactive: 提示用户会点 Done，但也可以选择主动 report
            # 两种类型都能用 send.py 中途汇报
            sub_system_prompt = self._build_subagent_system_prompt(task_type)

            # 在用户 prompt 外包一层强制通信协议（system prompt 优先级不够，
            # 任务 prompt 里直接写"协议头"和"协议尾"才能稳定生效）
            wrapped_prompt = self._wrap_child_prompt(prompt, task_type, parent_workspace)

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
        proc = run_info._process
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
                run_info._loop.call_soon_threadsafe(run_info._new_output_event.set)
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
            # 进程还在跑，标记完成
            run_info.status = RunStatus.COMPLETED
            run_info.completed_at = datetime.now()
            run_info.output_lines.append(f"[Agent OS] Task reported complete: {result[:100]}")
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

    def continue_run(self, run_id: str, prompt: str, source: str = "user",
                     model: str | None = None) -> bool:
        """在已有会话上追加一轮对话。可指定 model 覆盖当前会话的模型。"""
        run_info = self.runs.get(run_id)
        if not run_info:
            return False
        if run_info._process and run_info._process.poll() is None:
            return False
        if not run_info.session_id:
            return False

        # sanitize prompt 防止 surrogate 进入内存
        prompt = prompt.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

        # 模型选择：优先用参数指定的，再用 run_info 原有的，最后用默认值
        effective_model = model if model is not None else (run_info.model or self.default_model)

        run_info.turn_markers.append((len(run_info.output_lines), prompt))
        run_info.output_lines.append("")
        run_info.output_lines.append(f"─── Turn {len(run_info.turn_markers)} ───")
        run_info.output_lines.append(f"> {prompt}")
        run_info.output_lines.append("")
        # 结构化事件
        run_info.add_event("turn", index=len(run_info.turn_markers))
        run_info.add_event("prompt", text=prompt, role="user", source=source)

        cmd = self._build_cmd(prompt, resume_session=run_info.session_id, model=effective_model,
                              system_prompt=run_info.system_prompt)
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
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=workspace_cwd,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            shell=USE_SHELL,
            env=env,
            )

        run_info.status = RunStatus.RUNNING
        run_info.completed_at = None
        run_info.exit_code = None
        run_info._process = process
        run_info._new_output_event = asyncio.Event()

        self._start_reader(run_info)
        self._mark_dirty()
        return True

    def stop_run(self, run_id: str) -> bool:
        """终止子进程。"""
        run_info = self.runs.get(run_id)
        if not run_info or not run_info._process:
            return False

        if run_info.status == RunStatus.RUNNING:
            run_info._process.terminate()
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
                run_info._loop.call_soon_threadsafe(run_info._new_output_event.set)
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
        if run_info.status == RunStatus.RUNNING and run_info._process:
            try:
                run_info._process.terminate()
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
        del self.runs[run_id]
        return deleted + 1

    # ---------- rewind ----------

    @staticmethod
    def _cwd_to_session_key(cwd: str) -> str:
        """把 cwd 转成 CLI 的项目 key（实测自 `~/.<cli>/projects/` 下的目录名）。

        规则：
          - Windows 盘符：开头 `X:` → 小写盘符 + 直接去掉冒号（不替换为 `-`）
            例：`g:\\svn\\...` → `g\\svn\\...`、`C:\\Users\\...` → `c\\Users\\...`
          - 路径分隔符 `\\` `/` → `-`
          - 保留 `.`（`.agent_os` 不会被吃掉）
          - 去除首尾 `-`
        在非 Windows 平台（cwd 不以盘符开头），上述盘符规则不会触发，
        `/home/u/foo` → `home-u-foo`，与 CLI 实际目录命名相符。
        """
        import re
        if len(cwd) >= 2 and cwd[1] == ":" and cwd[0].isalpha():
            cwd = cwd[0].lower() + cwd[2:]
        key = re.sub(r"[\\/]", "-", cwd)
        return key.strip("-")

    def _session_root_candidates(self) -> list[str]:
        """候选的 CLI 会话根目录列表，按优先级排序。

        - 首选：基于 `cli_command` 推断出的目录名（codebuddy → `.codebuddy`、claude → `.claude`）
        - 兜底：扫 `~` 下所有 `.<name>/projects/` 目录
        - 环境变量 `AGENT_OS_CLI_HOME` 可强制指定（绝对路径，指向 projects/ 的父目录）
        """
        home = os.path.expanduser("~")
        roots: list[str] = []
        # 1) 环境变量强制覆盖
        env_root = os.environ.get("AGENT_OS_CLI_HOME")
        if env_root and os.path.isdir(os.path.join(env_root, "projects")):
            roots.append(env_root)
        # 2) 根据 CLI 名字推断
        cli_basename = os.path.basename(self.cli_command or "").lower()
        # 去掉常见后缀
        for ext in (".cmd", ".exe", ".bat", ".js"):
            if cli_basename.endswith(ext):
                cli_basename = cli_basename[: -len(ext)]
        if cli_basename:
            guess = os.path.join(home, f".{cli_basename}")
            if os.path.isdir(os.path.join(guess, "projects")):
                roots.append(guess)
        # 3) 兜底：扫 ~/ 下所有 .<name>/projects/
        try:
            for name in os.listdir(home):
                if not name.startswith("."):
                    continue
                p = os.path.join(home, name)
                if os.path.isdir(os.path.join(p, "projects")) and p not in roots:
                    roots.append(p)
        except OSError:
            pass
        return roots

    def _locate_session_jsonl(self, cwd: str, session_id: str) -> str | None:
        """在 CLI 会话目录里找 `<key>/<session_id>.jsonl`。

        策略：
          1. 计算项目 key（cwd → key）
          2. 依次在每个候选根目录下查 `<root>/projects/<key>/<session_id>.jsonl`
          3. 若 key 不命中，则在每个 root 下扫所有 project 子目录看是否含该 session_id.jsonl
             （应对 CLI key 算法在某些边缘 cwd 上和我们的实现略有差异）
        """
        key = self._cwd_to_session_key(cwd)
        filename = f"{session_id}.jsonl"
        for root in self._session_root_candidates():
            projects = os.path.join(root, "projects")
            # 精确匹配
            path = os.path.join(projects, key, filename)
            if os.path.exists(path):
                return path
        # 兜底：暴搜（session_id 是 UUID，命中后必定就是它）
        for root in self._session_root_candidates():
            projects = os.path.join(root, "projects")
            try:
                for proj in os.listdir(projects):
                    candidate = os.path.join(projects, proj, filename)
                    if os.path.exists(candidate):
                        logger.warning(
                            f"_locate_session_jsonl: key mismatch — expected={key} "
                            f"actual={proj} (found by session_id scan)"
                        )
                        return candidate
            except OSError:
                continue
        return None

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
        jsonl_path = self._locate_session_jsonl(cwd, ri.session_id)
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

        # output_lines / messages：保守起见全部清空再让 SSE 重新从 events 渲染；
        # output_lines 主要给文本回放用，前端实际渲染走 output_events，所以清空安全
        ri.output_lines.clear()
        # messages 是子 agent send.py 推上来的进度，rewind 不影响这层语义，保留
        # （要是从一个被 spawn 过的父 agent rewind，那确实可能多余，但删除会丢信息）

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
                        cwd=wdir, capture_output=True, text=True, timeout=10
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
                ri._loop.call_soon_threadsafe(ri._new_output_event.set)
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
        jsonl_path = self._locate_session_jsonl(cwd, ri.session_id)
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
        ri.output_lines.clear()
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
                ri._loop.call_soon_threadsafe(ri._new_output_event.set)
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
                    cwd=git_cwd, capture_output=True, text=True, timeout=15
                )
                r_commit = subprocess.run(
                    ["git", "commit", "-m",
                     f"[checkout:{self.recorder._ws_id(ws)}:{step_id}] reset {len(affected)} step(s) to pending"],
                    cwd=git_cwd, capture_output=True, text=True, timeout=15
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
        # 2. 扫描 workspaces/ 目录下的 runs/ 子目录
        workspaces_dir = os.path.join(self.project_root, ".agent_os", "workspaces")
        if os.path.isdir(workspaces_dir):
            for ws_name in os.listdir(workspaces_dir):
                ws_path = os.path.join(workspaces_dir, ws_name)
                runs_dir = os.path.join(ws_path, "runs")
                if os.path.isdir(runs_dir) and run_id in os.listdir(runs_dir):
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
                "output_length": len(ri.output_lines),
                "turns": len(ri.turn_markers),
            })
        return result

    def get_tree(self) -> list[dict]:
        """返回 agent 树结构（只返回根节点，children 嵌套）。"""
        roots = [ri for ri in self.runs.values() if not ri.parent_run_id]
        return [self._build_tree_node(ri) for ri in roots]

    @staticmethod
    def _unwrap_task_prompt(prompt: str) -> str:
        """从被 _wrap_child_prompt 包装的 prompt 中提取真实任务文本。

        优先取 [Your Task]...[/Your Task] 中间内容；否则剥掉通信协议头/收尾段
        后取首行。供树视图标题与 resume_parent 子任务摘要共用，避免把
        '[Agent OS Communication Protocol]' 协议头当成任务描述。"""
        import re as _re
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

        # 提取可读标题：优先 [Your Task]...[/Your Task]，否则取 prompt 首行
        display_prompt = self._unwrap_task_prompt(run_info.prompt or "")

        return {
            "run_id": run_info.run_id,
            "prompt": display_prompt[:120],
            "label": run_info.label,
            "status": run_info.status.value,
            "session_id": run_info.session_id,
            "started_at": run_info.started_at.isoformat(),
            "completed_at": run_info.completed_at.isoformat() if run_info.completed_at else None,
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
                await asyncio.wait_for(run_info._new_output_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

    def _on_run_completed(self, run_info: RunInfo) -> None:
        """当一个 run 完成时，检查是否需要触发 resume。"""
        # 找到所有关联的 spawn 请求
        for spawn_req in self.spawn_requests.values():
            if spawn_req.is_resolved:
                continue
            if run_info.run_id in spawn_req.child_run_ids:
                spawn_req.completed_children.add(run_info.run_id)
                self._check_spawn_resolution(spawn_req)

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
            # 直接在后台线程中执行 resume（spawn 新进程是线程安全的）
            import threading
            threading.Thread(
                target=self.resume_parent,
                args=(spawn_req,),
                daemon=True,
                name=f"resume-{spawn_req.parent_run_id[:6]}",
            ).start()

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

            task_desc = self._unwrap_task_prompt(child.prompt)[:100]
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
        parent_alive = parent._process is not None and parent._process.poll() is None

        if parent_alive:
            # 父 agent 进程还在运行，但无法通过 stdin 注入新对话。
            # 先 terminate 父进程，再用 continue_run 恢复（让父 agent
            # 看到子 agent 的结果并继续工作）。
            logger.info(f"resume_parent: parent {parent_run_id[:8]} still running (pid={parent._process.pid}), "
                        f"terminating and using continue_run")
            try:
                parent._process.terminate()
                parent._process.wait(timeout=5)
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

    def _build_cmd(self, prompt: str, agent_name: str | None = None,
                   resume_session: str | None = None,
                   system_prompt: str | None = None,
                   session_id: str | None = None,
                   model: str | None = None) -> list[str]:
        """构建 claude CLI 命令。

        - session_id: 首次启动时由 OS 主动指定（确保 OS 能可靠 resume）
        - resume_session: 恢复已有会话时传入
        - model: 指定模型（如 sonnet / opus / haiku 或完整模型名）；None 用 CLI 默认

        使用 cli_prefix 而非直接 cli_command，以便在 Windows 上绕过 .CMD shim
        对含换行 prompt 的截断 bug。
        """
        cmd = list(self.cli_prefix) + [
            "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json",
            "--verbose",
        ]
        if model:
            cmd.extend(["--model", model])
        if agent_name:
            cmd.extend(["--agent", agent_name])
        if resume_session:
            cmd.extend(["--resume", resume_session])
        elif session_id:
            # 首次启动且未 resume：由 OS 指定 session_id
            cmd.extend(["--session-id", session_id])
        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])
        return cmd

    @staticmethod
    def _sanitize_workspace_name(name: str) -> str:
        """把外部传入的 workspace 名清洗成单层安全目录名。

        仅作通用安全处理（防路径穿越/非法字符），不含任何业务语义：
        - 取 basename，剥离任何目录分隔，杜绝 ``../`` 穿越；
        - 非 ``[A-Za-z0-9._-]`` 的字符替换为 ``_``；
        - 去掉首尾 ``.``，为空时回退为 ``workspace``。
        """
        import os
        import re
        base = os.path.basename(str(name).strip().replace("\\", "/").rstrip("/"))
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip(".")
        return safe or "workspace"

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
            dir_name = self._sanitize_workspace_name(workspace_name) if workspace_name else run_id
            workspace_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "workspaces", dir_name
            )
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

    def _wrap_child_prompt(self, user_task: str, task_type: str = "generative",
                            workspace_path: str | None = None) -> str:
        """
        给子 agent 的用户 prompt 外包一层强制通信协议。
        generative: 告知 send.py + report.py，引导自行调用 report.py 结束。
        interactive: 只告知 send.py，report.py 完全去除，用户点 Done 才结束。
        workspace_path: 可选的 workspace 绝对路径，用于在 prompt 中告知 agent workspace 位置。
        """
        # 清除 surrogate 字符，避免传给子进程时乱码
        user_task = user_task.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

        # 从 workspace 路径提取任务名（最后一段目录名）
        ws_name = ""
        if workspace_path:
            ws_name = os.path.basename(workspace_path.rstrip("/\\"))
            ws_rel = os.path.join(".agent_os", "workspaces", ws_name)
        else:
            ws_rel = ".agent_os/workspaces/<任务名>/"

        if task_type == "interactive":
            header = (
                "[Agent OS Communication Protocol]\n"
                "You are running in an isolated subprocess. Your normal text output is "
                "NOT visible to the parent agent. You communicate with the parent via:\n"
                "  - python .agent_os/send.py --msg \"...\"     (send progress to parent)\n\n"
                "IMPORTANT - Workspace:\n"
                f"  Your shared workspace is at: {ws_rel}  #AOS_WS\n"
                "  All files you create/read should be placed in the workspace directory.\n"
                "  Use the workspace path directly or cd to it.\n\n"
                "IMPORTANT - Completion:\n"
                "  This is an INTERACTIVE task. Do NOT call report.py. "
                "The user will click Done in the dashboard when satisfied.\n"
                "[/Agent OS Communication Protocol]\n\n"
                "[Your Task]\n"
            )
        else:
            header = (
                "[Agent OS Communication Protocol]\n"
                "You are running in an isolated subprocess. Your normal text output is "
                "NOT visible to the parent agent. You communicate with the parent via:\n"
                "  - python .agent_os/send.py --msg \"...\"     (intermediate progress)\n"
                "  - python .agent_os/report.py --result \"...\"  (signal task completion)\n\n"
                "IMPORTANT - Workspace:\n"
                f"  Your shared workspace is at: {ws_rel}  #AOS_WS\n"
                "  All files you create/read should be placed in the workspace directory.\n"
                "  Use the workspace path directly or cd to it.\n\n"
                "When to call report.py:\n"
                "  - If your task is AUTOMATED (no ongoing user interaction needed): "
                "call report.py EXACTLY ONCE as your very last action.\n"
                "[/Agent OS Communication Protocol]\n\n"
                "[Your Task]\n"
            )
        footer = "\n[/Your Task]"

        return header + user_task + footer

    @staticmethod
    def _build_subagent_system_prompt(task_type: str = "generative") -> str:
        """
        生成注入到子 agent 的 system prompt。
        generative: 引导 agent 自行调用 report.py 结束。
        interactive: 不提及 report.py，用户点 Done 才结束。
        """
        if task_type == "interactive":
            return (
                "You are a sub-agent launched by an orchestrator (parent agent) "
                "running under Agent OS.\n\n"
                "## Communication\n\n"
                "Your normal response text is **NOT visible** to the parent. Use:\n\n"
                "- `python .agent_os/send.py --msg \"...\"` — send intermediate progress (parent stays asleep)\n\n"
                "## Completion\n\n"
                "This is an **interactive task** (ongoing discussion/Q&A with user). "
                "Do NOT call `report.py`. The user will click Done in the dashboard "
                "when they are satisfied. Use `send.py` freely throughout.\n"
            )
        return (
            "You are a sub-agent launched by an orchestrator (parent agent) "
            "running under Agent OS.\n\n"
            "## Communication\n\n"
            "Your normal response text is **NOT visible** to the parent. Use these scripts:\n\n"
            "- `python .agent_os/send.py --msg \"...\"` — send intermediate progress (parent stays asleep)\n"
            "- `python .agent_os/report.py --result \"...\"` — signal task completion (wakes the parent)\n\n"
            "## When to call report.py\n\n"
            "**Automated task** (no ongoing user interaction): call `report.py` EXACTLY ONCE "
            "as your very last action with a concise summary.\n\n"
            "The task prompt will indicate which type applies.\n"
        )

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
        """后台线程：逐行读取子进程 stdout。"""
        process = run_info._process
        logger.debug(f"[{run_info.run_id[:8]}] Reader started")
        try:
            line_count = 0
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                line = line.rstrip("\n\r")
                if not line:
                    continue
                # 清除 surrogate 字符，防止 json 序列化失败
                line = line.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                line_count += 1

                self._extract_session_id(run_info, line)
                if line_count == 1:
                    logger.debug(f"[{run_info.run_id[:8]}] First line processed, session_id={run_info.session_id is not None}")

                events = self._parse_stream_json_events(line)
                for ev in events:
                    kind = ev.get("kind", "raw")
                    # 同步 output_lines（兼容历史接口 / 文本回放）
                    if kind == "text":
                        for sub in ev.get("text", "").split("\n"):
                            run_info.output_lines.append(sub)
                    elif kind == "tool_use":
                        run_info.output_lines.append(f"> [{ev.get('tool')}] {ev.get('summary','')}")
                    elif kind == "tool_result":
                        for sub in ev.get("text", "").split("\n"):
                            run_info.output_lines.append(sub)
                    elif kind == "raw":
                        run_info.output_lines.append(ev.get("text", ""))
                    # 推结构化事件（自动唤醒 SSE）
                    payload = {k: v for k, v in ev.items() if k != "kind"}
                    run_info.add_event(kind, **payload)

            process.wait()
            run_info.exit_code = process.returncode
            logger.info(f"[{run_info.run_id[:8]}] Process exited: code={process.returncode}, status={run_info.status.value}, session_id={run_info.session_id is not None}, lines={line_count}")

            if run_info.status == RunStatus.RUNNING:
                if run_info.interactive:
                    logger.debug(f"[{run_info.run_id[:8]}] Interactive - staying running")
                else:
                    if not run_info.reported_result and hasattr(run_info, '_fallback_result') and run_info._fallback_result:
                        run_info.reported_result = run_info._fallback_result
                        logger.debug(f"[{run_info.run_id[:8]}] Using fallback_result")
                    run_info.status = (
                        RunStatus.COMPLETED if process.returncode == 0
                        else RunStatus.FAILED
                    )
                    run_info.completed_at = datetime.now()

                    # 打 turn 级 commit（每次对话轮次完成）
                    turn_num = len(run_info.turn_markers)
                    if run_info.workspace_path:
                        try:
                            self.recorder.turn_done(
                                run_info.run_id, turn_num, run_info.workspace_path
                            )
                        except Exception:
                            pass

                    logger.info(f"[{run_info.run_id[:8]}] Marked {run_info.status.value}, calling _on_run_completed")
                    self._on_run_completed(run_info)

                    # 记忆层：进程自然退出，记录完成（仅在未通过 report.py 报告时）
                    if run_info.workspace_path and not run_info._recorded:
                        run_info._recorded = True
                        try:
                            final = run_info.reported_result or run_info._fallback_result or "(无输出)"
                            self.recorder.run_done(
                                run_id=run_info.run_id,
                                result=final,
                                workspace_path=run_info.workspace_path,
                            )
                        except Exception:
                            pass
            elif run_info.status == RunStatus.WAITING:
                # 父 agent spawn 后进程退出了。检查子 agent 是否已完成：
                # - 已全部完成 → 标记 COMPLETED（resume 会在子 agent 完成时触发）
                # - 未完成 → 保持 WAITING，等待子 agent 完成后 resume
                all_children_done = True
                for spawn_req in self.spawn_requests.values():
                    if spawn_req.parent_run_id == run_info.run_id and not spawn_req.is_resolved:
                        all_children_done = False
                        break
                if all_children_done:
                    # 所有 spawn request 已 resolve（子 agent 已完成），
                    # resume_parent 已经走过消息路线，父 agent 自己退出
                    run_info.status = (
                        RunStatus.COMPLETED if process.returncode == 0
                        else RunStatus.FAILED
                    )
                    run_info.completed_at = datetime.now()
                    logger.info(f"[{run_info.run_id[:8]}] WAITING agent exited (all children done), marked {run_info.status.value}")
                    if run_info.workspace_path and not run_info._recorded:
                        run_info._recorded = True
                        try:
                            final = run_info.reported_result or run_info._fallback_result or "(无输出)"
                            self.recorder.run_done(
                                run_id=run_info.run_id,
                                result=final,
                                workspace_path=run_info.workspace_path,
                            )
                        except Exception:
                            pass
                else:
                    # 子 agent 还在运行，保持 WAITING，等子 agent 完成后 resume
                    logger.info(f"[{run_info.run_id[:8]}] WAITING agent exited but children still running, "
                               f"keeping WAITING for resume")
            else:
                logger.debug(f"[{run_info.run_id[:8]}] Status already {run_info.status.value}, skip exit handler")

        except Exception as e:
            run_info.add_text_line(f"[ERROR] {e}", kind="error")
            run_info.status = RunStatus.FAILED
            run_info.completed_at = datetime.now()
            self._on_run_completed(run_info)

            # 记忆层：记录异常完成（仅首次）
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
                run_info._loop.call_soon_threadsafe(run_info._new_output_event.set)

    @staticmethod
    def _extract_session_id(run_info: RunInfo, line: str) -> None:
        """从 stream-json 中对账 session_id 并提取 result（fallback）。

        OS 已通过 --session-id 主动指定 session_id，此处仅做对账验证：
        如果 stream-json 回报的 session_id 与预设不一致，记录警告。
        """
        try:
            obj = _json.loads(line)
        except (ValueError, TypeError):
            logger.debug(f"[{run_info.run_id[:8]}] Not JSON: {line[:80]}")
            return

        # 对账 session_id（OS 已预设，此处仅做验证）
        sid = obj.get("session_id")
        if sid and run_info.session_id and sid != run_info.session_id:
            logger.warning(
                f"[{run_info.run_id[:8]}] session_id mismatch: "
                f"preset={run_info.session_id[:13]}, cli_reported={sid[:13]}"
            )
            # 以 CLI 实际报告的为准（防止 CLI 内部生成了不同的 id）
            run_info.session_id = sid

        # 从 result 事件中提取最终回答（作为 fallback）
        if obj.get("type") == "result":
            result_text = obj.get("result", "")
            if result_text and not run_info.reported_result:
                run_info._fallback_result = result_text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    @staticmethod
    def _parse_stream_json_events(line: str) -> list[dict]:
        """解析 stream-json 一行，返回结构化事件列表。"""
        try:
            obj = _json.loads(line)
            # sanitize：json.loads 可能把 \uDxxx 转义解析成 Python surrogate 字符
            obj = ProcessManager._sanitize(obj)
        except (ValueError, TypeError):
            stripped = line.strip()
            if not stripped:
                return []
            return [{"kind": "raw", "text": line}]

        msg_type = obj.get("type", "")
        events: list[dict] = []

        if msg_type == "assistant":
            message = obj.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type", "")
                    if bt == "text":
                        text = block.get("text", "")
                        if text:
                            events.append({"kind": "text", "text": text})
                    elif bt == "tool_use":
                        tool_name = block.get("name", "?")
                        tool_input = block.get("input", {}) or {}
                        event = {"kind": "tool_use", "tool": tool_name}
                        if tool_name == "Bash":
                            event["summary"] = tool_input.get("command", "")
                        elif tool_name == "Write":
                            file_path = tool_input.get("file_path", "")
                            event["summary"] = file_path
                            event["file_path"] = file_path
                            event["content"] = tool_input.get("content", "")
                        elif tool_name == "Edit":
                            file_path = tool_input.get("file_path", "")
                            event["summary"] = file_path
                            event["file_path"] = file_path
                            event["old_string"] = tool_input.get("old_string", "")
                            event["new_string"] = tool_input.get("new_string", "")
                        elif tool_name == "Read":
                            event["summary"] = tool_input.get("file_path", "")
                        elif tool_name == "Grep":
                            event["summary"] = tool_input.get("pattern", "")
                        elif tool_name == "Glob":
                            event["summary"] = tool_input.get("pattern", "")
                        elif tool_name == "TodoWrite":
                            todos = tool_input.get("todos", [])
                            event["summary"] = f"{len(todos)} todo(s)"
                            # 透传 todos 列表给前端，便于渲染清单视图
                            if isinstance(todos, list):
                                event["todos"] = todos
                        else:
                            event["summary"] = _json.dumps(tool_input, ensure_ascii=False)[:200]
                        events.append(event)
            return events

        if msg_type == "user":
            # 工具结果回传
            message = obj.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    result_content = block.get("content", "")
                    text = ""
                    if isinstance(result_content, list):
                        parts = []
                        for rc in result_content:
                            if isinstance(rc, dict) and rc.get("type") == "text":
                                parts.append(rc.get("text", ""))
                        text = "\n".join(parts)
                    elif isinstance(result_content, str):
                        text = result_content
                    if not text:
                        continue
                    truncated = False
                    if len(text) > 800:
                        text = text[:800] + "\n... (truncated)"
                        truncated = True
                    events.append({
                        "kind": "tool_result",
                        "text": text,
                        "truncated": truncated,
                    })
            return events

        # system / result：忽略（result 已在 _extract_session_id 中作为 fallback 捕获）
        return []
