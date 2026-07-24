# Agent OS 架构文档

> 分支：`refactor/decouple-layers`
> 仓库根：`g:\svn\trunk_cn\dragon\game\.agent_os`（junction 真实路径 `G:\svn\trunk_cn\tools\AI-for-Coding\server\server_act_workspace\agent_os\.agent_os`）

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│  Dashboard (浏览器)                                       │
│  ├── Agent 树形视图   ├── 结构化事件终端                   │
│  ├── Workspace 文件浏览 ├── 录制记录 + git diff            │
│  └── 交互操作（Continue/Done/Stop/Delete/Rewind/Clear）    │
└───────────┬─────────────────────────────────────────────┘
            │ SSE + REST API
┌───────────▼─────────────────────────────────────────────┐
│  FastAPI Backend (dashboard/app.py + routers/)           │
└───────────┬─────────────────────────────────────────────┘
            │
┌───────────▼─────────────────────────────────────────────┐
│  ProcessManager (门面/组装器)                              │
│  ├── EventBus          发布/订阅总线                       │
│  ├── Registry          树状态注册表                        │
│  ├── Orchestrator      定型+编排决策                       │
│  │   ├── GoalGraph     Goal 评估循环（LangGraph）          │
│  │   └── SupervisorGraph Supervisor 审查循环（LangGraph）  │
│  ├── PromptBuilder     参数组装（纯函数）                   │
│  ├── RunStateMachine   状态转换校验（transitions 库）       │
│  ├── backend           进程后端（Native/SDK/Omnigent）     │
│  └── recorder          git 记忆层                          │
└───────────┬─────────────────────────────────────────────┘
            │ subprocess.Popen
┌───────────▼─────────────────────────────────────────────┐
│  CLI 子进程 (codebuddy/claude)                            │
│  独立上下文，自带工具/权限/skill                            │
│  通过 stdout(stream-json) + HTTP(report.py/send.py) 通信   │
└─────────────────────────────────────────────────────────┘
```

## 二、模块分层

```
┌─────────────────────────────────────────────────────┐
│  ⑤ 门面层                                              │
│     process_manager.py                                │
├─────────────────────────────────────────────────────┤
│  ④ 推理层（LangGraph）                                  │
│     goal_graph.py    supervisor_graph.py              │
├─────────────────────────────────────────────────────┤
│  ③ 编排层                                              │
│     orchestrator.py                                   │
├─────────────────────────────────────────────────────┤
│  ② 业务逻辑层                                           │
│     registry.py   prompt_builder.py   run_state_machine│
├─────────────────────────────────────────────────────┤
│  ① 基础设施层                                           │
│     models.py   event_bus.py   dag_planner.py          │
│     cli_resolver.py   stream_parser.py                 │
└─────────────────────────────────────────────────────┘
```

**单向依赖**：基础设施 ← 业务逻辑 ← 编排 ← 门面。无反向依赖。

## 三、各模块详解

### ① 基础设施层

#### `models.py` — 数据模型

定义所有核心数据结构。纯数据，不含业务逻辑。

```python
class RunStatus(str, Enum):
    RUNNING / COMPLETED / FAILED / STOPPED / WAITING / PLAN_PENDING

class RunInfo(BaseModel):
    # 可序列化字段
    run_id, prompt, session_id, status, parent_run_id, children_run_ids,
    output_events, reported_result, goal, interactive, task_type,
    workspace_path, step_id, system_prompt, goal_retries, supervisor, ...
    # 运行时字段（__init__ 里 object.__setattr__ 声明）
    _session, _bus, _fallback_result, _recorded, _event_seq,
    _new_output_event, _active_supervisor, _waiting_supervisor,
    _max_goal_retries, _supervisor_done, _goal_graph_active, _supervisor_graph_active

    def add_event(kind, **payload)  # 追加事件 + bus.publish

class SpawnRequest(BaseModel):
    spawn_id, parent_run_id, parent_session_id, child_run_ids,
    wait_strategy, completed_children, is_resolved

@dataclass
class CompletionSignal:
    run_id, exit_code, reported_result, source
```

**依赖**：无（纯 pydantic + stdlib）

#### `event_bus.py` — 事件总线

进程内发布/订阅，解耦 RunInfo 与 SSE/持久化。

```python
class EventBus:
    subscribe(topic, handler)    # 订阅
    publish(topic, **payload)    # 发布（线程安全，inspect.iscoroutinefunction 分发）
    # Topic: "run.event" / "run.dirty" / "run.completion"
```

**关键**：RunInfo.add_event 不再直接唤醒 SSE/标脏，而是 `bus.publish("run.event")` + `bus.publish("run.dirty")`。StreamOutput 和 Persistence 各自订阅。

**依赖**：无（纯 stdlib）

#### `dag_planner.py` — DAG 拓扑排序（已有，非重构）

DAG 节点的拓扑排序、就绪查询、状态标记。纯函数。

#### `cli_resolver.py` / `stream_parser.py`（已有，非重构）

CLI 路径解析、stream-json 协议解析。

### ② 业务逻辑层

#### `registry.py` — 树状态注册表

runs dict + 父子关系 + SpawnRequest 批次的增删改查。纯数据操作。

```python
class Registry:
    runs: dict[str, RunInfo]
    spawn_requests: dict[str, SpawnRequest]

    # 基本存取
    register(ri) / unregister(id) / get(id)

    # 树查询
    list_runs() -> list[dict]
    get_tree() -> list[dict]           # 嵌套树结构
    _build_tree_node(ri) -> dict

    # Spawn 批次
    link_spawn(spawn_id, parent, children, strategy) -> SpawnRequest
    get_spawn(spawn_id)
    restore_spawn_requests()           # 从 runs 重建

    # 工具
    @staticmethod unwrap_task_prompt(prompt, system_prompt) -> str
```

**依赖**：models.py

**关键**：ProcessManager 通过 `@property runs` / `@property spawn_requests` 转发到 Registry。

#### `prompt_builder.py` — 参数组装

把任务描述组装成启动配置。纯计算，零 IO。

```python
class PromptBuilder:
    @staticmethod build_root_system_prompt(workspace_path) -> str
    @staticmethod build_subagent_system_prompt(task_type, task_prompt, workspace_path) -> str
    @staticmethod build_work_context(run_info) -> str
```

**依赖**：models.py（只读 RunInfo）

#### `run_state_machine.py` — 状态转换校验

用 transitions 库做状态转换合法性校验。不附加到 RunInfo（避免 pydantic 冲突）。

```python
class RunStateMachine:
    STATES = ['running', 'completed', 'failed', 'stopped', 'waiting', 'plan_pending']
    TRANSITIONS = [  # 13 条合法转换
        {'trigger': 'complete', 'source': 'running', 'dest': 'completed'},
        ...
    ]

    @classmethod can_transition(from_status, to_status) -> bool  # 线程安全
    @classmethod get_graph()  # 可视化状态图
```

**依赖**：transitions 库

**关键**：ProcessManager._transition() 内部调 `RunStateMachine.can_transition()` 校验。

### ③ 编排层

#### `orchestrator.py` — 定型 + 编排决策

唯一的"大脑"。进程退出后定型 + 决定下一步。

```python
class Orchestrator:
    def __init__(self, pm)          # 持有 ProcessManager 引用
    def __getattr__(self, name)     # 自动转发 self.xxx → self._pm.xxx

    def resolve_process_exit(run_info, exit_code)
        # PLAN_PENDING → 不动
        # RUNNING + interactive → 不动
        # RUNNING + reported_result → COMPLETED + DAG/recorder
        # RUNNING + parent + supervisor → 特判
        # RUNNING + parent + 无 report → FAILED
        # RUNNING + 根 agent → COMPLETED/FAILED
        # WAITING → all_children_done 判定

    def on_run_completed(run_info)
        # 1. supervisor 分支 → SupervisorGraph
        # 2. goal 分支 → GoalGraph
        # 3. spawn resolution → resume_parent
```

**依赖**：通过 `__getattr__` 转发到 ProcessManager

**关键**：ProcessManager 的 `_resolve_process_exit` 和 `_on_run_completed` 委托到 Orchestrator。

### ④ 推理层（LangGraph）

#### `goal_graph.py` — Goal 评估循环

用 LangGraph StateGraph 表达 evaluate→feedback→evaluate 循环。

```python
class GoalState(TypedDict):
    run_id, goal, retries, max_retries, is_met, eval_reason

class GoalGraph:
    # 节点
    _evaluate(state)    # 调 pm._evaluate_goal 评估
    _feedback(state)    # 反馈 + interrupt 等 agent 重做
    _route(state)       # 达成→END，未达成→feedback

    # 接口
    run(run_id, goal, max_retries) -> bool     # 首次启动
    resume(run_id) -> bool                      # agent 重做后恢复

    # 持久化
    checkpointer = SqliteSaver(state/goal_graph.db)  # 重启可恢复
```

**依赖**：langgraph + ProcessManager

**中断点**：1 个（feedback 节点 interrupt 等 agent 重做）

**集成**：Orchestrator.on_run_completed 的 goal 分支调 `run()` / `resume()`

#### `supervisor_graph.py` — Supervisor 审查循环

双 agent 循环（审查者 + 被审查者交替）。

```python
class SupervisorState(TypedDict):
    agent_run_id, supervisor_run_id, verdict, correction_feedback, review_round

class SupervisorGraph:
    # 节点
    _spawn_supervisor(state)  # 创建 supervisor
    _wait_verdict(state)      # interrupt 等 PASS/CORRECTION
    _correct(state)           # CORRECTION → 反馈 + interrupt 等 agent 重做
    _route(state)             # PASS→END，CORRECTION→correct

    # 接口
    run(agent_run_id) -> bool
    resume_supervisor(agent_run_id, verdict) -> bool
    resume_agent(agent_run_id) -> bool

    # 持久化
    checkpointer = SqliteSaver(state/supervisor_graph.db)  # 重启可恢复
```

**依赖**：langgraph + ProcessManager

**中断点**：2 个（_wait_verdict 等 supervisor + _correct 等 agent 重做）

**集成**：
- Orchestrator.on_run_completed 的 supervisor 分支调 `run()` / `resume_agent()`
- report_complete 的 PASS/CORRECTION 调 `resume_supervisor()`（保留 fallback 兼容旧数据）

### ⑤ 门面层

#### `process_manager.py` — ProcessManager

组装所有模块 + 提供对外接口。自身不再有业务逻辑（退化为委托）。

```python
class ProcessManager:
    # 组装
    self._bus = EventBus(loop)
    self._registry = Registry()
    self._orchestrator = Orchestrator(self)
    self._goal_graph = GoalGraph(self)
    self._supervisor_graph = SupervisorGraph(self)
    self._backend = get_backend(...)
    self.recorder = Recorder(...)

    # property 转发（兼容）
    @property runs → self._registry.runs
    @property spawn_requests → self._registry.spawn_requests

    # 委托方法
    get_tree() → self._registry.get_tree()
    list_runs() → self._registry.list_runs()
    _build_work_context() → PromptBuilder.build_work_context()
    _resolve_process_exit() → self._orchestrator.resolve_process_exit()
    _on_run_completed() → self._orchestrator.on_run_completed()

    # 自身保留（进程管理 + IO）
    start_run() / continue_run() / stop_run()     # 进程生命周期
    _read_output()                                 # 读取 stream-json
    _start_reader()                                # reader 线程
    stream_output()                                # SSE 推送
    complete_interactive() / report_complete()     # 完成触发
    _transition()                                  # 状态转换（用 RunStateMachine 校验）
    _on_run_event()                                # EventBus 订阅者
    _mark_dirty() / _periodic_save_worker()        # 持久化
```

## 四、Agent 生命周期

### 核心循环

```
1. 派活    start_run：起子进程，建 RunInfo，起 reader 线程
2. 监听    reader 线程：读 stream-json → RunInfo.add_event() → bus → SSE
3. 交活    agent 调 report.py（HTTP）或进程退出
4. 判断    _resolve_process_exit：定型（transition）
5. 编排    on_run_completed：supervisor? → goal? → spawn resolution?
6. 下一步  resume_parent 或结束
```

### 状态转换

```
RUNNING → COMPLETED    进程正常退出 / agent 调 report.py / 用户点 Done
RUNNING → FAILED       进程异常退出 (exit_code≠0)
RUNNING → STOPPED      用户手动终止 / rewind / clear
RUNNING → WAITING      spawn_children，等子 agent
RUNNING → PLAN_PENDING agent 调 ExitPlanMode，等用户审批
WAITING → RUNNING      resume_parent 唤醒
WAITING → COMPLETED    所有子完成 + 父进程已退出
PLAN_PENDING → RUNNING 用户 approve plan
STOPPED → RUNNING      continue_run（rewind/clear 后重新输入）
任意完成态 → STOPPED    rewind
```

所有转换走 `transition()` 唯一入口，用 `RunStateMachine.can_transition()` 校验。

### 完成处理（四个触发源）

| 触发源 | 场景 | 行为 |
|---|---|---|
| `_read_output` | 进程退出 | 调 `_resolve_process_exit` 定型 |
| `report_complete` | report.py HTTP 回调 | 设 reported_result + resume graph 或 _on_run_completed |
| `complete_interactive` | 用户点 Done / idle 超时 | COMPLETED + _on_run_completed |
| `_timeout_watcher` | idle 超时 | 调 complete_interactive |

### 编排决策（on_run_completed 三分支，按顺序）

```
1. supervisor 分支：
   首次 → SupervisorGraph.run() → 创建 supervisor → interrupt 等审查
   agent 重做 → SupervisorGraph.resume_agent() → resume supervisor 审查
   PASS → 清除 supervisor → 继续 spawn resolution

2. goal 分支：
   首次 → GoalGraph.run() → evaluate → 未达成 → feedback + interrupt
   agent 重做 → GoalGraph.resume() → evaluate → ...
   达成/超限 → 清除 goal → 继续 spawn resolution

3. spawn resolution：
   遍历 spawn_requests → all/any 满足 → resume_parent
```

### 父子关系（spawn）

```
父 agent (Turn 1) 跑 spawn.py
  → OS 起多个子进程，建 SpawnRequest，父置 WAITING
  → 各子 agent 各走自己的生命周期
  → 每个子完成都进 on_run_completed
  → SpawnRequest 判定 all/any 满足
  → resume_parent：汇总子结果 → --resume 唤醒父 (Turn 2)
```

## 五、通信机制

### 三条通信边界

```
A. OS ──启动参数──> Agent      (prompt + 环境变量 + session_id，一次性)
B. Agent ──stdout──> OS        (reader 线程持续读 stream-json)
B'.Agent ──HTTP POST──> OS     (report.py / send.py 主动回调)
C. 人 ──REST/SSE──> OS         (启动/继续/停止/看进度)
```

### 调用脚本

| 脚本 | 通信方式 | 用途 |
|---|---|---|
| `spawn.py` | HTTP POST `/api/spawn` | 派发子 agent |
| `report.py` | HTTP POST `/api/run/{id}/report` | 汇报最终结果 |
| `send.py` | HTTP POST `/api/run/{id}/send` | 发送中间消息 |
| `dag.py` | 直接读写 `dag.json` 文件 | DAG 编排状态管理 |

根目录的 `spawn.py` / `report.py` 是 shim（转发到 `src/scripts/`）。`dag.py` / `send.py` 因功能差异保留独立。

## 六、持久化

### 三层持久化

| 层 | 存储 | 内容 | 重启恢复 |
|---|---|---|---|
| RunInfo | `state/runs.json` + sqlite | run 元数据 + 事件流 | ✓（只读，不能 resume 进程） |
| Graph checkpoint | `state/goal_graph.db` / `state/supervisor_graph.db` | graph 状态（retries/verdict） | ✓（SqliteSaver 持久化） |
| Git 记忆层 | `.agent_os/` 独立 git 仓库 | agent 产出文件 | ✓（baseline/step/turn commit） |

### 重启恢复流程

```
OS 重启
  → load_runs_from_disk：RunInfo 从 sqlite 恢复
  → _restore_spawn_requests：从 runs 重建 SpawnRequest
  → _resume_restored_parents：恢复 WAITING 状态的 run
  → agent 再次完成时：
    _goal_graph_active=True → GoalGraph.resume()（SqliteSaver 恢复 checkpoint）
    _supervisor_graph_active=True → SupervisorGraph.resume_agent()
```

### Git 记忆层 commit 结构

```
[task:<ws>:baseline]    任务启动时
[step:<ws>:<step_id>]   每个 DAG step 完成时
[agent:<ws>:<run_id>]   agent 完成时
[turn:<ws>:<run_id>:N]  每轮对话完成时
[checkout:<ws>:<step>]  回退时
```

## 七、测试

### 独立测试文件（每个模块可单独运行）

| 文件 | 模块 | 测试数 |
|---|---|---|
| `test_event_bus.py` | EventBus | 6 |
| `test_registry.py` | Registry | 8 |
| `test_prompt_builder.py` | PromptBuilder | 6 |
| `test_run_state_machine.py` | RunStateMachine | 12 |
| `test_orchestrator.py` | Orchestrator | 8 |
| `test_supervisor_graph.py` | SupervisorGraph | 7 |
| `test_goal_graph.py` | GoalGraph | 9 |
| `test_dag_planner.py` | dag_planner | 31 |
| `test_agent_lifecycle.py` | ProcessManager 生命周期 | 14 |
| `test_spawn_children.py` | spawn 流程 | 19+ |

**总计**：130 passed, 15 skipped, 0 failed

## 八、依赖

### Python 包

| 包 | 用途 |
|---|---|
| `fastapi` + `uvicorn` | Dashboard 后端 |
| `pydantic` | 数据模型 |
| `transitions` | 状态转换校验 |
| `langgraph` + `langchain-core` | 推理层 StateGraph |
| `langgraph-checkpoint-sqlite` | Graph checkpoint 持久化 |

### 目录结构

```
.agent_os/
├── src/core/
│   ├── models.py              数据模型
│   ├── event_bus.py           事件总线
│   ├── registry.py            树状态注册表
│   ├── prompt_builder.py      参数组装
│   ├── run_state_machine.py   状态转换校验
│   ├── orchestrator.py        定型+编排
│   ├── goal_graph.py          Goal 评估 LangGraph
│   ├── supervisor_graph.py    Supervisor 审查 LangGraph
│   ├── process_manager.py     门面/组装器
│   ├── dag_planner.py         DAG 拓扑排序
│   ├── cli_resolver.py        CLI 路径解析
│   └── stream_parser.py       stream-json 解析
├── src/agent/
│   └── backend.py             进程后端（Native/SDK/Omnigent）
├── src/persistence/
│   ├── sqlite.py              RunInfo 持久化
│   └── git_recorder.py        Git 记忆层
├── src/mcp/
│   └── server.py              MCP Server
├── dashboard/
│   ├── app.py                 FastAPI 入口
│   ├── routers/               REST 路由
│   └── templates/             Dashboard UI
├── tests/
│   ├── test_event_bus.py      独立测试
│   ├── test_registry.py
│   ├── test_prompt_builder.py
│   ├── test_run_state_machine.py
│   ├── test_orchestrator.py
│   ├── test_supervisor_graph.py
│   ├── test_goal_graph.py
│   └── ...                    集成测试
├── state/
│   ├── runs.json              Run 元数据
│   ├── goal_graph.db          Goal graph checkpoint
│   └── supervisor_graph.db    Supervisor graph checkpoint
├── spawn.py / report.py       根目录 shim
├── dag.py / send.py           根目录独立脚本
├── main.py                    启动入口
└── requirements.txt
```
