# Agent OS

多智能体任务编排系统。在 AI Agent 之上提供多 agent 协同、DAG 流水线、会话管理。

## 快速开始

```bash
.agent_os\setup.bat   # 一键配置环境
.agent_os\start.bat   # 启动
# 浏览器打开 http://127.0.0.1:8420
```

## 切换底层 Agent

编辑 `.agent_os/cli_config.json`：

```json
{"cli": "codebuddy", "backend": "native"}           // CLI 模式（默认）
{"cli": "codebuddy", "backend": "codebuddy-sdk"}    // SDK 模式
```

重启即生效。

## 主要功能

### Agent 调度

直接调度 CodeBuddy CLI，充分利用其原生的 Harness 设计（工具调用、权限控制、MCP 集成等），不做阉割。

### 多 Agent 协同

支持两种 Agent 类型：
- **生成式**：自动执行任务，完成后通过 `report.py` 汇报结果，自动 resume 父 Agent
- **交互式**：等待用户在 Dashboard 中确认完成

主 Agent 可动态 spawn 子 Agent 并行工作，最多 3 层嵌套。子 Agent 通过 `send.py` 向父 Agent 发送进度消息。

### Goal 评估

为 Agent 设置目标描述，完成后自动评估是否达成。未达成则注入反馈自动重试，可配置最大重试次数。

评估通过独立的 Agent 做语义判断，不依赖规则匹配。评估上下文来自 Agent 的输出事件、report 结果和进度消息。

```json
// API 示例
{"goal": "Agent must create a hello.py that prints Hello World", "max_goal_retries": 3}
```

### Supervisor 监督

为 Agent 设置一个监督者，每轮对话结束后审查产出。监督者保持独立会话，能看到完整的审查历史，逐步指导 Agent 修正。

与 Goal 的区别：
- **Goal** = 裁判：判断是否达成，不达成丢理由重试
- **Supervisor** = 导师：持续对话，逐步审查，能记住之前的纠正意见

在 Dashboard 中勾选 Supervisor 并填写审查标准即可启用。监督者的提示词会作为 system prompt 注入，确保被严格遵守。

### DAG 任务流水线

定义多步骤工作流（`dag.json`），声明步骤间的依赖关系。调度 Agent 自动按拓扑序并行执行就绪步骤，结合 Goal 评估判断每个 step 是否完成，未完成自动重试。

支持回退到任意 step — 重置该 step 及下游状态，代码文件通过 Git 恢复到该 step 的快照，重新调度执行。

### Workspace 文件记忆

每个任务有独立的 workspace 目录，所有 Agent 的文件操作都在此目录内。通过 Git 自动记录每次变更（Turn / Agent / Step 三级 commit），可回溯任意时间点的文件状态。回退时通过 fork 新分支实现，不影响原始记录。

## 周边功能

### 流式输出

Agent 输出实时推送到浏览器，Markdown 渲染 + 代码高亮 + 工具调用折叠 + 文件变更 diff。

### 会话回退

随时回退到对话的任意位置，底层截断对话历史文件，回退后 Agent 自动"忘记"被截断的内容。

## 依赖

- Python 3.10+
- CodeBuddy CLI
- Git

## 实现细节

### Agent 调度

底层通过统一的 `AgentBackend` 协议抽象，支持 CLI（subprocess）和 SDK（进程内调用）两种模式。CLI 模式直接启动 CodeBuddy 进程，通过 stdout 管道逐行读取 stream-json 输出；SDK 模式通过 `codebuddy-agent-sdk` 在后台线程中运行，事件直接推入内存队列。

两种模式切换只需改配置，上层业务代码不感知差异。

### 多 Agent 协同

**Spawn 流程**：
1. 父 Agent 使用 CodeBuddy 原生 **Task 工具**创建子 Agent。OS 通过 PreToolUse Hook 拦截 Task 调用，自动转发到内部 spawn API
2. OS 为每个子 Agent 创建独立会话，注入专用的 system prompt（包含任务描述、workspace 路径）
3. 子 Agent 继承父 Agent 的 workspace 目录，共享文件上下文
4. 父 Agent 状态标记为 `WAITING`，等待子 Agent 完成

**Resume 流程**：
1. 子 Agent 完成后，OS 自动检测并通过 `--resume` 恢复父 Agent 会话
2. OS 注入子 Agent 的结果摘要，父 Agent 可继续工作或再次 spawn
3. 支持 `all` / `any` 等待策略

**Task Hook**：子 Agent 创建通过 Task 工具的 PreToolUse Hook 实现，不需要 MCP。Hook 脚本在 `.agent_os/src/hooks/task_hook.py`，拦截 Task 调用后 POST 到 `/api/spawn`。

**System Prompt 注入**：每个子 Agent 启动时，OS 自动注入：
- 当前 workspace 目录路径
- 父 Agent 传递的任务描述
- 完成后自动被 OS 检测并 resume 父 Agent

### DAG 流水线

**数据结构**：`dag.json` 定义 step 列表，每个 step 包含 id、prompt、depends_on、status（pending/running/done/failed）。

**调度 Agent 的 System Prompt**：启动 DAG 时，OS 自动为调度 Agent 注入 system prompt，包含：
- 完整的 step 列表及依赖关系
- 执行指令：`dag.py --ready` 取就绪节点 → Task 工具派发子 Agent → 等待 OS resume → `dag.py --mark-done <id>` 标记完成 → 循环
- 完成后调用 `report.py` 汇报的指引

**调度算法**：使用 Python 标准库 `graphlib.TopologicalSorter` 做拓扑排序，支持环检测。`ready_steps()` 返回所有依赖已满足且状态为 pending 的 step。

**执行流程**：
1. 调度 Agent 读取 dag.json，通过 `dag.py --ready` 获取就绪 step
2. 对就绪 step 用 Task 工具批量创建子 Agent，传入 step_id、goal、supervisor
3. 子 Agent 完成后，OS 自动调用 `dp.mark_done()` 更新 dag.json 状态，打 Git step commit
4. 调度 Agent 被 resume，继续下一轮

**Goal 评估**：每个 step 可设置 goal，子 Agent 完成后 OS 启动一个短暂的评估 Agent 判断是否达成。未达成则自动给反馈重试，达到最大重试次数后标记为 failed。评估 Agent 通过 `AgentBackend.evaluate()` 做语义判断，上下文来自 output_events、report 和进度消息。

**Supervisor 监督**：`_on_run_completed` 中 supervisor 优先于 goal 执行。首次调用 `launch(session_id=xxx)` 创建监督者会话，后续 `launch(resume_session=xxx)` 复用同一会话，让监督者看到完整的审查历史。用户的监督提示词作为 system_prompt 注入，prompt 中强调"必须严格对照 system_prompt 中的标准审查，全部满足才能 PASS"。监督者无次数上限，每次 agent 完成都会审查。

**回退**：通过 `recorder.checkout_step()` 找到 step commit，`git checkout` 到其父 commit，fork 新分支。同时调用 `dp.reset_steps()` 将该 step 及下游重置为 pending。

### Workspace 文件记忆

**共享上下文**：同一任务下的所有 Agent（父 Agent 和所有子 Agent）共享同一个 workspace 目录。子 Agent 可以直接读取父 Agent 产出的文件，也可以写入新文件供后续 Agent 使用，形成协作的工作流。

**Git 仓库**：在 `.agent_os/` 下维护独立 Git 仓库，不污染用户项目。

**三层 Commit**：
- `[turn:<ws>:<run>:N]` — 每轮对话完成时
- `[agent:<ws>:<run>]` — 每个 Agent 完成时
- `[step:<ws>:<step_id>]` — DAG step 完成时（含 dag.json 状态变更）

**分支管理**：每个 workspace 对应一个基准分支。回退操作通过 `fork_branch_locked()` 创建衍生分支（命名如 `xxx-r1`），基准分支永远不变。

### 流式输出

**数据流**：Agent 进程 stdout → `backend.stream()` → `parse_stream_json_events()` 解析为结构化事件 → `RunInfo.output_events` 内存缓存 → `asyncio.Event` 唤醒 → FastAPI SSE → 浏览器 EventSource。

SDK 模式下跳过 stdout 管道和 JSON 解析，直接推送事件 dict，减少一次序列化开销。

**前端渲染**：按事件 kind 分发 — text/text_delta 做 Markdown 渲染和打字机效果，tool_use 做可折叠详情（Write/Edit 带 diff 视图），tool_result 做截断显示。

### 会话回退

**存储**：对话历史存储在 `~/.codebuddy/projects/<key>/<session_id>.jsonl`，每行一个 JSON 记录。

**回退流程**：
1. 找到目标 user prompt 对应的 seq
2. 在 jsonl 文件中找到匹配行，截断文件（备份 `.bak`）
3. 清空内存中的 `output_events`
4. Git 回退到对应 commit 的父 commit
5. 下次 continue 时 Agent 读到截断后的历史，自然"忘记"被截内容

## Agent 规范

### Agent 类型

| 类型 | 行为 | 适用场景 |
|------|------|---------|
| `generative` | 自动执行，完成后调用 `report.py` 结束 | DAG step、子任务 |
| `interactive` | 等待用户在 Dashboard 点 Done | 需要人工确认的任务 |
| `explore` | 不能 spawn 子 agent | 探索/分析任务 |

### 注入的 System Prompt

每种 Agent 启动时，OS 自动注入以下 system prompt。

**根 Agent / 子 Agent**（`_build_root_system_prompt` / `_build_subagent_system_prompt`）：

```
You are running under Agent OS, a multi-agent orchestration system.

## Workspace

Your workspace is at .agent_os/workspaces/<your_run_id>/.
This is the persistent file memory for the entire task.

## Agent Types (for spawning children)

- generative: runs autonomously, calls report.py when done
- interactive: waits for user to click Done in the dashboard

## Available Tools

- Create sub-agents: use the Task tool (subagent_type=generative|interactive)
  to spawn child agents. Sub-agents share your workspace.
- report.py: `python .agent_os/report.py --result "<summary>"`
  Call this when your task is done. Your parent agent will be resumed.
- send_message: use the SendMessage tool to send progress updates.
```

**DAG 调度 Agent**（`dag.py` 端点注入）：

```
你是 DAG 调度 agent。按模板顺序执行流水线。

执行方式：
1. dag.py --ready → 取就绪节点（含 id/prompt/type/goal/supervisor）
2. 对每个就绪节点创建子 agent，传入 prompt/goal/supervisor，末尾追加 step_id
3. 派发完结束对话，等 OS resume
4. resume 后 dag.py --mark-done <id>，循环
5. --ready 返回空时 report.py --result "全部完成"
```

**Supervisor Agent**（`_run_supervisor` 注入）：

```
system_prompt:
  You are a strict supervisor reviewing an AI agent's work.
  Your job is to verify that the agent's output meets ALL criteria:
  {用户自定义的审查标准}
  Only reply PASS if every criterion is fully satisfied.

prompt:
  ## Current Task
  {goal 或 prompt}
  ## Agent Output (Turn N)
  {_build_work_context 收集的上下文}
  ## Instructions
  严格审查，全部满足才 PASS，有问题回 CORRECTION
```

### 子 Agent 创建方式

父 Agent 通过 **Task 工具**创建子 Agent，OS 通过 PreToolUse Hook 拦截 Task 调用，转发到 `/api/spawn` 端点。子 Agent 创建后继承父 Agent 的 workspace，共享文件上下文。

Task 工具调用参数通过 `SpawnTask` 模型定义：`prompt`、`agent_name`、`type`、`model`、`step_id`、`goal`、`supervisor`。

### 子 Agent 完成与 Resume

子 Agent 调用 `report.py` 后，OS 自动检测完成状态。当所有子 Agent 完成（或 `any` 策略下任意一个完成），OS 通过 `--resume` 恢复父 Agent 会话，注入子 Agent 结果摘要。
