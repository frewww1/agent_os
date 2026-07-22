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

### Workspace 文件记忆

每个任务独立 workspace，所有 Agent 文件操作共享。Git 自动记录三级 commit，可回溯任意时间点、回退到任意 Step 重新执行。

- **共享上下文**：父 Agent spawn 的所有子 Agent 读写同一个 workspace，无需手动传递文件
- **三级 Commit**：Turn（每轮对话）→ Agent（每个 Agent 完成）→ Step（DAG step 完成）
- **回退恢复**：回退到任意 step 时从 Git 提取文件快照，fork 新分支，历史记录不丢失
- **持久化**：重启后 workspace 目录和 Git 历史完整保留

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

为 DAG step 设置审查标准，step 完成后 OS 自动启动一个**可见的 supervisor agent** 审查产出：

1. 执行 agent 调 `report.py` → OS 检测到有 supervisor → 不直接完成，spawn supervisor agent
2. supervisor agent 在 Dashboard 中可见，可查看执行 agent 的产出
3. **全部通过** → `report.py --result "PASS"` → OS 自动唤醒执行 agent，标记 step 完成
4. **有问题** → `send.py --msg "CORRECTION: <反馈>"` → OS 立即 resume 执行 agent 修正，supervisor 结束。下轮自动 resume 同一 supervisor 会话继续审查
5. 用户也可手动在 Dashboard 介入审查、点 Done

与 Goal 的区别：
- **Goal** = 裁判：判断是否达成，不达成丢理由重试
- **Supervisor** = 导师：可见会话，跨轮次持久化 session，能记住之前的纠正意见

### DAG 任务流水线

定义多步骤工作流（`dag.json`），声明步骤间的依赖关系。调度 Agent 自动按拓扑序并行执行就绪步骤，结合 Goal 评估判断每个 step 是否完成，未完成自动重试。

支持回退到任意 step — 重置该 step 及下游状态，代码文件通过 Git 恢复到该 step 的快照，重新调度执行。

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
1. 父 Agent 调用 `python spawn.py --tasks '[...]'` → POST 到 `/api/spawn`
2. OS 为每个子 Agent 创建独立会话，注入专用的 system prompt（含 How to Complete、任务描述、workspace 路径）
3. 子 Agent 独立工作空间（可继承父 workspace），共享文件上下文
4. 父 Agent 状态标记为 `WAITING`，等待子 Agent 完成

**Resume 流程**：
1. 子 Agent 完成后 OS 自动检测，全部子 agent 完成时 resume 父 Agent 会话
2. OS 注入子 Agent 的结果摘要，父 Agent 可继续工作或再次 spawn
3. 支持 `all` / `any` 等待策略

**System Prompt 注入**：每个子 Agent 启动时 OS 自动注入 workspace 路径、## How to Complete 指令、工具说明、父 Agent 传递的任务描述。

### DAG 流水线

**数据结构**：`dag.json` 定义 step 列表，每个 step 包含 id、prompt、depends_on、status（pending/running/done/failed）。

**调度 Agent 的 System Prompt**：启动 DAG 时自动注入，包含完整 step 列表及依赖关系、执行指令（`dag.py --ready` → `spawn.py` 派发 → 等 resume → `dag.py --mark-done` 循环）、完成指引。

**调度算法**：使用 Python 标准库 `graphlib.TopologicalSorter` 做拓扑排序，支持环检测。`ready_steps()` 返回所有依赖已满足且状态为 pending 的 step。

**执行流程**：
1. 调度 Agent 读取 dag.json，通过 `dag.py --ready` 获取就绪 step
2. 对就绪 step 用 `spawn.py` 批量创建子 Agent，传入 step_id、type、goal、supervisor
3. 子 Agent 完成后，OS 自动调用 `dp.mark_done()` 更新 dag.json 状态，打 Git step commit
4. 调度 Agent 被 resume，继续下一轮

**Goal 评估**：每个 step 可设置 goal，子 Agent 完成后 OS 启动一个短暂的评估 Agent 判断是否达成。未达成则自动给反馈重试，达到最大重试次数后标记为 failed。评估 Agent 通过 `AgentBackend.evaluate()` 做语义判断，上下文来自 output_events、report 和进度消息。

**Supervisor 监督**：执行 agent 调 report.py 后，`_spawn_supervisor()` 创建 visible的子 agent，通过 `_waiting_supervisor` 标记等待。supervisor 调 `report.py --result "PASS"` 时 `report_complete` 检测到 supervisor → 清除等待标记 → 调用 `_on_run_completed` 做 spawn resolution 唤醒父 agent。CORRECTION 路径则是 supervisor 先 `send_message` → `send_message` handler 清除等待标记并 `continue_run` 执行 agent → supervisor 再 `report.py` 结束自己。

**回退**：通过 `recorder.checkout_step()` 找到 step commit，`git checkout` 到其父 commit，fork 新分支。同时调用 `dp.reset_steps()` 将该 step 及下游重置为 pending。

### Workspace 文件记忆

**共享上下文**：同一任务下的所有 Agent（父 Agent 和所有子 Agent）共享同一个 workspace 目录（`.agent_os/workspaces/<name>/`）。子 Agent 可以直接读取父 Agent 产出的文件，也可以写入新文件供后续 Agent 使用，无需手动传递文件路径。

**Git 仓库**：每个 workspace 在 `.agent_os/` 下维护独立 Git 仓库，不污染用户项目。`Recorder` 类封装所有 Git 操作，提供统一的 commit 接口。

**三级 Commit**：
- `[turn:<ws>:<run>:N]` — 每轮对话完成时（`turn_done`）
- `[agent:<ws>:<run>]` — 每个 Agent 完成时（`run_done`），包含最终 report 结果
- `[step:<ws>:<step_id>]` — DAG step 完成时（`step_done`），含 dag.json 状态变更

**基线策略**：首次进入 workspace 时自动创建 Git 仓库和 `.gitkeep` 基线 commit（`[task:<ws>:baseline]`）。`ensure_task_branch()` 检查基准分支存在性，不存在则创建。

**分支管理**：每个 workspace 对应一个基准分支（如 `feature-dag-r0`）。回退操作通过 `fork_branch_locked()` 创建衍生分支（命名如 `feature-dag-r1`），基准分支永远不变。`_checkout_branch()` 支持自动切换到已有分支，避免重复创建。

**回退实现**：`checkout_step()` 根据 step_id 找到对应的 step commit，调用 `git checkout <parent_commit>` 回退文件到该 step 之前的状态，同时 fork 新分支保留历史。文件回退后 Agent 看到的是干净的代码状态。

**持久化**：workspace 目录和 Git 仓库的 `.git/` 都在磁盘上，重启后完整保留。DAG 调度 Agent resume 时直接读取 workspace 中的文件继续工作。

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

## How to Complete

根据自身类型，完成方式不同：

**generative agent**：
- ⚠️ 必须调用 `python .agent_os/report.py --result "<摘要>"` 完成任务
- 不调 report.py 直接退出 → OS 标记为 **FAILED**
- 用户随时可点 **Done** 手动完成

**interactive agent**：
- ⚠️ 必须等用户在 Dashboard 点 **Done** 才能完成
- 调用 report.py 会被忽略
- 通常用于需要用户输入/确认的步骤

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
2. 对每个就绪节点用 spawn.py 创建子 agent：
   python spawn.py --tasks '[{"prompt":"...","type":"<类型>","step_id":"<id>",...}]'
3. 派发完结束对话，等 OS resume
4. resume 后 dag.py --mark-done <id>，循环
5. --ready 返回空时 report.py --result "全部完成"
```

**Supervisor Agent**（`_spawn_supervisor` 创建）：

```json
// system_prompt
"你是严格审查 AI agent 工作的监督者。验证 agent 产出是否满足以下标准：
{用户标准}
全部满足 → report.py --result \"PASS\"
有问题 → send_message \"CORRECTION: <反馈>\" 再 report.py --result \"done\""

// prompt
"## 审查任务
{任务描述}

## Agent 产出
{执行 agent 输出}

## 指令
全部满足 → report.py --result \"PASS\"
有问题 → send_message \"CORRECTION: ...\" 再 report.py --result \"done\""
```

### 子 Agent 创建方式

父 Agent 通过 `spawn.py` 脚本创建子 agent：`python spawn.py --tasks '[{...}]'` → POST 到 `/api/spawn`。子 agent 继承父 agent 的 workspace，共享文件上下文。

Task 工具调用参数通过 `SpawnTask` 模型定义：`prompt`、`agent_name`、`type`（或 `subagent_type`）、`model`、`step_id`、`goal`、`supervisor`。

### 子 Agent 完成与 Resume

子 agent 调 `report.py` 后，OS 自动检测完成。全部子 agent 完成时 OS resume 父 agent，注入子 agent 结果摘要。有 supervisor 时，PASS 唤醒执行 agent → spawn resolution，CORRECTION 先 send_message 再 resume 执行 agent 修正。

---

## 附录：所有 Agent System Prompt

### DAG 调度 Agent

由 `dag.py` 端点创建，注入到调度 agent 的 system_prompt：

```
你是 DAG 调度 agent。按模板顺序执行流水线：

  {step 列表及依赖关系}

执行方式：
1. `python .agent_os/dag.py --ready` → 取就绪节点（JSON 数组，含 id/prompt/type/goal/supervisor）
2. 对每个就绪节点，用 spawn.py 创建子 agent：
   `python .agent_os/spawn.py --tasks '[{"prompt":"...","type":"<interactive|generative>","step_id":"<节点id>","goal":"...","supervisor":"..."}]'`
   - 必须保留 --ready 返回的所有字段（prompt/type/goal/supervisor/step_id）
   - interactive: 子 agent 等用户在 Dashboard 点 Done
   - generative: 子 agent 自行调 report.py 结束
3. 派发完所有就绪节点后结束对话，等 OS 自动 resume
4. resume 后 `python .agent_os/dag.py --mark-done <id>`，回到第 1 步
5. --ready 返回空时 `python .agent_os/report.py --result "全部完成"`

Supervisor 机制：带 supervisor 的 step，子 agent 完成后 OS 自动启动审查 agent，有问题自动 CORRECTION 重试。
```

### Supervisor Agent

`_spawn_supervisor()` 创建，审查执行 agent 的产出：

**system_prompt**：
```
你是严格审查 AI agent 工作的监督者。
验证 agent 产出是否满足以下所有标准：

{用户自定义的审查标准}

Be critical and thorough.
All criteria met → `python report.py --result "PASS"`
Issues found → `python send.py --msg "CORRECTION: <feedback>"` to the agent.
Do NOT call report.py after sending feedback — just exit.
You will be resumed automatically for the next round of review.
```

**prompt**：
```
## 审查任务
{任务描述}

## Agent 产出
{执行 agent 的输出上下文}

## 指令
审查 agent 产出是否满足所有标准。
全部满足 → `python report.py --result "PASS"` 结束审查
有问题 → `python send.py --msg "CORRECTION: <具体问题>"` 告知执行 agent。不要调 report.py，直接结束即可，下一轮会被自动 resume。
```

### 子 Agent（DAG Step）

`_build_subagent_system_prompt()` 创建，根据 type 不同注入不同的完成指引：

**公共部分**：
```
You are a sub-agent running under Agent OS.

## Workspace
Your shared workspace is at: .agent_os/workspaces/<name>/
This is the persistent file memory for the entire task — all agents in this pipeline read and write to this same directory. Files you create here will be accessible to downstream agents.
```

**generative 完成指引**：
```
## How to Complete
You are a **generative** agent. You work autonomously and decide when to finish.
When your task is complete, you **must** call `python report.py --result "<summary>"` to report your results. Without this, your task will be marked as **failed** even if the work is done.
- ⚠️ report.py is MANDATORY for completion. The process exiting alone is not enough.
- The user can also click **Done** to manually complete you at any time.
```

**interactive 完成指引**：
```
## How to Complete
You are an **interactive** agent. Your task requires user input or confirmation.
When ready for user review, inform the user what you've done and what input you need. The user will click **Done** in the dashboard to mark your task as complete.
- ⚠️ Do NOT call report.py — it will be ignored. Only the Done button completes you.
```

**工具**：
```
## Available Tools
- Create sub-agents: use the Task tool (subagent_type=generative|interactive) for further parallel work.
- report.py: `python report.py --result "<summary>"` — call when task is done
- send.py: `python send.py --msg "<message>"` — send progress updates to parent

## Task
{父 agent 传入的任务描述}
```

### 根 Agent

`_build_root_system_prompt()` 创建：

```
You are running under Agent OS, a multi-agent orchestration system.

## Workspace
Your workspace is at .agent_os/workspaces/<your_run_id>/.
This is the persistent file memory for the entire task.

## Agent Types
- generative: runs autonomously, calls report.py when done
- interactive: waits for user to click Done in the dashboard
- explore: cannot spawn children, for exploration tasks

## Available Tools
- Create sub-agents: use the Task tool (subagent_type=generative|interactive) to spawn child agents. Sub-agents share your workspace.
- report.py: `python .agent_os/report.py --result "<summary>"`
- send.py: `python .agent_os/send.py --msg "<message>"`
```
