# Agent OS 重构计划

> 分支：`refactor/decouple-layers`
> 仓库根：`g:\svn\trunk_cn\dragon\game\.agent_os`（junction 真实路径 `G:\svn\trunk_cn\tools\AI-for-Coding\server\server_act_workspace\agent_os\.agent_os`）
> 基点：`d5c963735c`

## 核心约束

**重构期间绝对不修改功能，保持行为不变。** 每步靠 `tests/` 绿灯验证，通过才进下一步。

## 现状诊断

`process_manager.py` 2216 行，一个类 50+ 方法，混了 8 类职责。Bug 易出的根因：

1. **隐式状态机** — `RunStatus` 6 态，转换散落在 ~15 个方法。`_on_run_completed` 里 supervisor/goal/spawn 三路分支交织。
2. **幽灵字段** — `_active_supervisor` / `_waiting_supervisor` / `_max_goal_retries` 靠 `object.__setattr__` 和 `getattr(..., None)` 动态挂接，全代码库无声明处。
3. **并发三路混用** — threading（persist-worker、reader、resume fallback）、asyncio（SSE、timeout_watcher）、`run_in_executor` 并存。`_check_spawn_resolution` 有 loop 走 asyncio、没 loop 走线程——测试与生产行为不一致。
4. **持久化多真相源** — 内存 dict + 全局 runs.json + 分片 runs.json + git 回退后手动清内存。
5. **数据模型耦合基础设施** — `RunInfo.add_event()` 里塞了 `_loop` / `_new_output_event` / `_dirty_callback`，直接唤醒 SSE 和标脏持久化。

## 目标架构（七模块，依赖单向无环）

```
                    ┌──────────────┐
                    │  API 路由层   │  (薄 HTTP 入口)
                    └──────┬───────┘
                           │ 调用
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌───────────┐ ┌──────────┐
        │Orchestrator│ │StreamOutput│ │Persistence│
        │ (大脑)    │ │ (SSE)     │ │ (快照)    │
        └─────┬────┘ └─────┬─────┘ └─────┬────┘
              │            │             │
   起进程/订阅完成    订阅event      订阅dirty
              │            │             │
              ▼            ▼             ▼
        ┌──────────┐  ┌──────────────────────┐
        │AgentRunner│  │      EventBus        │  ← 全局订阅总线
        │ (门面)   │─┘                      │
        └─────┬────┘                         │
   读写RunInfo │                              │
              ▼                              │
        ┌──────────┐                         │
        │ Registry │ ────────────────────────┘
        │ (树状态)  │  (被所有人读写，不主动调任何人)
        └──────────┘
```

**关键性质**：EventBus 和 Registry 是底层（无依赖）；Persistence/StreamOutput 是纯订阅者（零业务逻辑）；AgentRunner 只向下依赖；Orchestrator 是唯一顶层消费者。没有任何反向依赖或环。

## 各模块详述

### 模块 1 — EventBus（基础设施）

- **职责**：进程内发布/订阅，线程安全调度 handler
- **高内聚**：只做事件分发，零业务逻辑
- **解耦点**：发布者不认识订阅者，订阅者不认识发布者
- **接口**：`subscribe(topic, handler)` / `publish(topic, **payload)`
- **依赖**：无（最底层）
- **代码来源**：新增 `src/core/event_bus.py`
- **Topic**：`run.event`（新事件）/ `run.dirty`（需持久化）/ `run.completion`（完成信号）

### 模块 2 — Registry（树状态）

- **职责**：`runs` dict 增删改查 + 父子关系维护 + SpawnRequest 批次追踪
- **高内聚**：纯数据结构操作，不关心进程/IO/持久化/编排
- **解耦点**：不定型状态（只存不改语义）、不持 backend、不调 bus、不主动持久化
- **接口**：`register(ri)` / `unregister(id)` / `get(id)` / `tree()` / `link_spawn(parent, children, strategy)` / `spawn_status(spawn_id)` / `children_of(id)`
- **依赖**：无（纯数据）
- **代码来源**：从 ProcessManager 拆 `runs`/`spawn_requests` 字段 + `get_tree`/`_build_tree_node`/`_find_workspace_for_run`/`_restore_spawn_requests`/`list_runs`/`get_run`

### 模块 3 — AgentRunner（门面，组合三子模块）

AgentRunner 进一步拆为三个高内聚子模块，三者只通过数据传递（`LaunchConfig` → `SessionHandle` → 事件流），不互相调用。

```
AgentRunner (薄门面/协调)
    ├─→ ① PromptBuilder     纯计算：组装启动参数
    ├─→ ② ProcessSupervisor IO：进程起/杀/等
    └─→ ③ StreamReader      IO：读输出 + 产 CompletionSignal
```

#### 3a — PromptBuilder（参数组装，纯函数）

- **职责**：把任务描述组装成启动配置：system_prompt + user_prompt 包装 + env 注入 + cli_args
- **高内聚**：纯计算，零 IO，零状态
- **解耦点**：不起进程、不读输出；输入任务描述，输出 `LaunchConfig` 数据对象
- **接口**：`build_root(task) -> LaunchConfig` / `build_subagent(task, parent_ctx) -> LaunchConfig` / `build_work_context(ri) -> str`
- **代码来源**：`_build_root_system_prompt` / `_build_subagent_system_prompt` / `_build_env` / `_build_work_context` / `_unwrap_task_prompt`
- **依赖**：无
- **拆分价值**：可独立单测；Orchestrator 决定 spawn 时可先构造/预览参数

#### 3b — ProcessSupervisor（进程生命周期，IO 层）

- **职责**：进程创建/终止/等待：`launch` → `SessionHandle`，`terminate`/`wait`/`kill`
- **高内聚**：纯 OS 进程操作，不关心输出内容、不关心参数怎么来的
- **解耦点**：起完进程就交出 handle，不读输出；输入 `LaunchConfig`，输出 `SessionHandle`
- **接口**：`launch(config) -> SessionHandle` / `terminate(handle)` / `wait(handle, timeout) -> int`
- **代码来源**：backend.launch/terminate/wait 的薄包装 + `_resolve_cli` + session_id 生成（`uuid4`）
- **依赖**：backend（已有抽象）
- **拆分价值**：可被复用——goal 评估、supervisor 启动都要起独立进程，共用同一进程管理层

#### 3c — StreamReader（输出读取 + 完成检测，IO + 翻译层）

- **职责**：迭代 `backend.stream` → 写结构化事件到 Registry → stream 结束时产 `CompletionSignal`
- **高内聚**：只管"读什么 + 何时算读完"，不关心为何起进程、不定型状态
- **解耦点**：不起进程（拿 handle 只管读）；不定型（只报 exit_code/reported_result/source 原始事实）；通过 bus publish 事件
- **接口**：`read(handle, run_id) -> CompletionSignal`（阻塞读完，或迭代事件）
- **代码来源**：`_start_reader` / `_read_output`（170 行，事件 kind 处理 + 完成判定）
- **关键改造**：`_read_output` 里所有定型逻辑（RUNNING→COMPLETED/FAILED、supervisor 特判、DAG 子 agent 失败判定）**全部删除**，改为收集事实塞进 CompletionSignal 交 Orchestrator
- **依赖**：backend.stream + EventBus（publish event/completion）+ Registry（写 output_events）
- **拆分价值**：bug 高发区，独立后可单测完成检测

### 模块 4 — Orchestrator（流程控制，唯一大脑）

- **职责**：状态机 + 编排决策（resume/spawn/stop）
- **高内聚**：唯一的定型者 + 唯一的编排决策者
- **解耦点**：订阅 CompletionSignal（不直接读进程）；通过 AgentRunner 接口起进程；通过 Registry 查树
- **接口**：`handle_completion(signal)` / `handle_user_done(id)` / `handle_report(id, result)` / `handle_spawn(parent, tasks, strategy)`
- **状态机**：显式转换表 + `transition(run, to_status)` 唯一入口，非法转换抛错
- **依赖**：Registry（查/改状态）+ AgentRunner（起进程）+ EventBus（订阅 completion）
- **代码来源**：从 ProcessManager 拆 `_on_run_completed`/`_check_spawn_resolution`/`resume_parent`/`complete_interactive`/`report_complete` 的定型部分 + `start_run`/`continue_run`/`spawn_children` 的编排部分
- **关键改造**：四个触发源（进程退出/report/用户Done/超时）改为只报 CompletionSignal，定型权全部收归此处
- **风险**：高（动逻辑最多），但有前 3 步测试网兜底

### 模块 5 — Persistence（持久化）

- **职责**：内存→磁盘快照 + 启动恢复
- **高内聚**：只关心怎么存/怎么恢复，不关心存什么、为什么变
- **解耦点**：纯订阅者，订阅 `run.dirty`，零业务逻辑
- **接口**：`snapshot()` / `restore() -> list[RunInfo]` + 自动订阅 dirty（节流 3s）
- **依赖**：Registry（读 runs 做快照）+ EventBus（订阅 dirty）
- **代码来源**：从 ProcessManager 拆 `_periodic_save_worker`/`_mark_dirty`/`save_runs_to_disk`/`load_runs_from_disk`/`_migrate_legacy_workspace_state`

### 模块 6 — StreamOutput（流输出）

- **职责**：SSE 推送
- **高内聚**：只把事件推给 HTTP 消费者
- **解耦点**：纯订阅者，订阅 `run.event`，零业务逻辑
- **接口**：`stream(run_id) -> AsyncGenerator[str]`（cursor 游标 + bus 唤醒）
- **依赖**：Registry（读 output_events）+ EventBus（订阅 event）
- **代码来源**：从 ProcessManager 拆 `stream_output`

### 模块 7 — API 路由层（薄入口）

- **职责**：HTTP 入口，翻译请求为模块调用
- **高内聚**：薄路由，无业务逻辑
- **解耦点**：只依赖上述模块接口
- **代码来源**：现有 `dashboard/routers/`，依赖对象从 ProcessManager 改为各模块

### ProcessManager 残留

瘦身为组装器：`__init__` 里 wire 各模块。自身不再有业务方法，或直接由 `main.py` 组装替代。

## 落地顺序（风险从低到高，每步独立可提交可测试）

| 步 | 模块 | 风险 | 验证标准 |
|---|---|---|---|
| 1 | EventBus | 零 | 新增文件，现有行为不变 |
| 2 | StreamOutput + Persistence 改订阅 | 低 | SSE 和落盘行为不变，`add_event` 不再直接操作 `_loop`/`_new_output_event`/`_dirty_callback` |
| 3 | Registry | 中低 | 所有 runs 读写改走接口，`get_tree`/`list_runs` 等行为不变 |
| 4a | PromptBuilder | 中 | 纯函数搬家，参数组装行为不变 |
| 4b | ProcessSupervisor | 中 | 进程启动/终止行为不变 |
| 4c | StreamReader | 中 | 输出读取行为不变，CompletionSignal 产出正确 |
| 5 | Orchestrator + 状态定型收敛 | 高 | 四触发源只报事实；状态转换走 transition 唯一入口；现有测试全过 |
| 6 | 清理重复脚本 + ProcessManager 瘦身 | 低 | 根目录 shim 收敛，组装逻辑清晰 |

每步结束跑 `tests/`（goal_supervisor 1309 行、spawn/lifecycle/backend 等已有覆盖），绿灯才进下一步。

## 关键改造点汇总

1. **状态定型收敛**：四个完成触发源（`_read_output`/`report_complete`/`complete_interactive`/idle 超时）改为只报 `CompletionSignal`（exit_code/reported_result/source 原始事实），定型权全部收归 Orchestrator 的 `transition()` 唯一入口。
2. **幽灵字段声明**：`_active_supervisor`/`_waiting_supervisor`/`_max_goal_retries` 全部声明进 `RunInfo`。
3. **数据模型去耦**：`RunInfo.add_event()` 不再直接操作 `_loop`/`_new_output_event`/`_dirty_callback`，改为 `EventBus.publish`。
4. **并发收敛**：消除 `_check_spawn_resolution` 的 asyncio/线程双路径。

## 现有 agent 封装位置（重构基础，不需改动）

- **进程抽象层**：`src/agent/backend.py`（925 行）— `SessionHandle` / `AgentBackend`(Protocol) / `BaseAgentBackend` / `NativeBackend` / `OmnigentBackend` / `SDKBackend` / `CodeBuddySDKBackend`。`launch` 返回 `SessionHandle`，`stream(handle)` 返回标准化事件迭代器。AgentRunner 三子模块在此之上做职责切分。
- **状态对象层**：`src/core/models.py` 的 `RunInfo` / `SpawnRequest`。
- **调用脚本**：`src/scripts/` + 根目录 `spawn.py`/`report.py`/`send.py`（agent 回调 OS 的薄 HTTP 客户端）。
