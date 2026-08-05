# Agent OS

多智能体任务编排系统。让多个 AI Agent 像团队一样协作——自动分工、审查、修正，直到任务完成。

## 亮点

- **Autonomous Loop 自主循环**：Agent 自主完成复杂任务的循环机制 — 执行 → 评估 → 决策 → 修正，无需人工介入，像 Reinforcement Learning 的 Agentic 实现
- **DAG Graph 编排图**：可视化 DAG 拓扑图，节点状态实时着色，依赖关系一目了然
- **DAG 任务流水线**：定义步骤依赖，自动并行执行。支持回退到任意 step，从中间重新跑
- **Supervisor 监督者**：为 Agent 指派专属监督者，持续审查产出并指导修正，记住上下文
- **Goal 目标评估**：设定任务目标，完成后自动语义判断是否达成，未达成自动重试
- **多 Agent 协同**：Agent 可动态 spawn 子 Agent，共享 workspace，最多 3 层嵌套

---

## 快速开始

### 环境要求

- Python 3.10+
- Git
- 任意一种 AI CLI（三选一）：
  - [CodeBuddy CLI](https://www.codebuddy.ai/)（推荐，原生支持）
  - [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)（通过 Native 模式支持）
  - 其他兼容 CLI

### 启动

```bash
.agent_os\setup.bat          # 一键配置环境
.agent_os\start.bat          # 启动服务
# 浏览器打开 http://127.0.0.1:8420
```

### 全局部署（按 CLI 运行目录工作）

agent_os 只需**部署在一个固定位置**，然后在任意项目目录下直接启动，它会以
**当前 CLI 运行目录**为工作根（project_root）——Agent 的 workspace、产物、状态
都落在当前目录，不用每个项目复制一份 `.agent_os`。

```bat
:: 1. 部署：把整个仓库放到固定位置，如 C:\tools\agent_os
:: 2. 把 C:\tools\agent_os\.agent_os 加入 PATH（该目录含 agent_os.cmd）
:: 3. 在任意项目目录下执行：
agent_os
:: 浏览器打开 http://127.0.0.1:8420，Agent 以当前目录为工作根
```

行为细节：

- 服务默认以 `os.getcwd()` 为 `project_root`，也可 `agent_os --root <路径>` 显式指定
- `state/`（agent 历史）与 `workspaces/`（任务产物）都创建在运行目录下，项目间互不干扰
- 启动时自动在运行目录下创建 `.agent_os` junction 指向安装目录，因此 Agent
  system prompt 中的 `python .agent_os/report.py` 等相对路径调用仍可直接工作
- 首次在新目录运行时，若旧版 state（安装目录 `state/`）存在会自动迁移过去，不丢历史

**Dashboard 内切换目录**：侧边栏底部显示当前工作根目录，点击右侧 `⇄` 打开切换面板
（`POST /api/root/switch`）。切换**不重建服务实例**——只是改 `project_root` 并重载
该目录的 agent 历史，后台线程/DB 连接均复用。前提是无运行中的 agent，否则会拒绝并提示。
切走再切回，原目录的历史与 workspace 原样保留。

切换面板提供智能候选（`GET /api/root/candidates`）：
- **最近访问**：切换历史自动记忆（存于安装目录 `state/root_history.json`），一键切回
- **常见项目**：自动扫描当前/历史目录的父目录，列出含 `.git`/`.codebuddy` 的项目目录
- 输入框实时过滤候选，Enter 确认、Esc 关闭

### 切换底层 Agent

编辑 `.agent_os/cli_config.json`：

```json
// 使用 CodeBuddy（推荐，支持 SDK 加速）
{"cli": "codebuddy", "backend": "native"}
{"cli": "codebuddy", "backend": "codebuddy-sdk"}

// 使用 Claude Code
{"cli": "claude", "backend": "native"}
```

Native 模式通过 subprocess 启动任意 CLI，理论上支持所有兼容 CodeBuddy/Claude 协议的 CLI 工具。重启生效。

---

## 核心功能

### Autonomous Loop 自主循环

像 Reinforcement Learning 的 Agentic 实现 — Agent 在自主循环中持续工作：执行子任务 → 评估结果 → 决策是否重试 → 修正后继续，直到所有 step 通过或达到上限。调度 Agent 通过 `autonomous-loop.py` 脚本管理循环状态，全程无需人工介入。

```
INIT → EXECUTE (spawn 子 Agent) → EVALUATE (判断结果)
         ↑                              ↓
         └──── RETRY (注入反馈) ←── FAIL/PASS ──→ 下一步 / 完成
```

### DAG Graph 编排图

前端内嵌 SVG 拓扑图，实时展示 DAG 步骤的依赖关系和执行状态。节点颜色随状态变化（pending / running / done / failed），一目了然。

### DAG 任务流水线

定义 `dag.json` 描述多步骤工作流，声明依赖关系。调度 Agent 自动按拓扑序并行执行就绪步骤。

```json
{
  "steps": [
    {"id": "design", "name": "设计", "prompt": "创建 index.html...", "depends_on": []},
    {"id": "coding", "name": "编码", "prompt": "实现页面逻辑...", "depends_on": ["design"]},
    {"id": "review", "name": "审查", "prompt": "审查代码质量...", "depends_on": ["coding"],
     "supervisor": "检查代码是否包含安全漏洞和性能问题"}
  ]
}
```

每个 step 可设置 `goal`（目标）和 `supervisor`（监督者）。调度 Agent 自动管理执行流程：

```
dag.py --ready → 取就绪节点 → 用 Task 工具创建子 Agent → 等待完成 → mark-done → 循环
```

#### 回退到任意 Step

可以随时从中间某个 step 重新跑，不用从头开始。选择目标 step 后自动完成：

1. **文件恢复**：workspace 文件回退到该 step 执行前的状态
2. **状态重置**：该 step 及下游全部重置为 pending
3. **继续调度**：调度 Agent 从该 step 重新执行

### Supervisor 监督者

为任意 Agent 设置一个监督者，每轮对话结束后自动审查产出。监督者保持独立会话，能看到完整审查历史，逐步指导修正。

与 Goal 的区别：

| | Goal | Supervisor |
|---|---|---|
| 角色 | 裁判：判断是否达成 | 导师：持续指导 |
| 会话 | 每次新 Agent | 复用同一会话，记住历史 |
| 次数 | 有上限 | 无上限 |
| 交互 | 一次判断 | 多轮对话 |

Dashboard 中勾选 Supervisor 并填写审查标准即可启用。监督者提示词作为 system prompt 注入，确保被严格遵守。

### Goal 目标评估

为 Agent 设置目标描述，完成后自动语义判断是否达成。未达成则注入反馈自动重试，可配置最大重试次数。Dashboard 中勾选 Goal 并填写目标即可启用。

### 多 Agent 协同

三种 Agent 类型：

| 类型 | 行为 |
|------|------|
| `generative` | 自动执行，完成后调 `report.py` 结束 |
| `interactive` | 等待用户在 Dashboard 点 Done |
| `explore` | 不能 spawn 子 Agent，用于探索任务 |

父 Agent 用 **Task 工具**创建子 Agent，OS 自动拦截并管理生命周期。子 Agent 继承父 Agent 的 workspace，共享文件上下文，最多 3 层嵌套。

### 其他功能

- **流式输出**：实时推送，Markdown 渲染 + 代码高亮 + 工具调用折叠 + diff 视图
- **会话回退**：截断对话历史文件，Agent 自动"忘记"被截内容
- **防重复提交**：发送按钮防抖，避免误创建多个 Agent
- **树状视图**：父子 Agent 关系一目了然

---

## 详细设计

### 架构

```
Dashboard (FastAPI + SSE)
    ↓
ProcessManager (多 Agent 调度核心)
    ↓
AgentBackend (统一协议)
    ├── NativeBackend (CLI subprocess)
    ├── CodeBuddySDKBackend (进程内 SDK)
    └── OmnigentBackend
```

`AgentBackend` 协议统一了 CLI 和 SDK 两种模式，上层业务代码不感知差异。

### DAG 调度

**数据结构**：`dag.json` 定义 step 列表，每个 step 含 id、prompt、depends_on、status、goal、supervisor。

**调度算法**：`graphlib.TopologicalSorter` 拓扑排序，支持环检测。`ready_steps()` 返回依赖已满足的 pending step。

**调度 Agent System Prompt**：OS 自动注入完整的 step 列表、执行指令、回退指引。

**执行流程**：

1. 调度 Agent 读取 dag.json → `dag.py --ready` 取就绪节点
2. 用 Task 工具批量创建子 Agent，传入 step_id、goal、supervisor
3. 子 Agent 完成后 OS 自动 `dp.mark_done()` + Git step commit
4. 调度 Agent 被 resume，继续下一轮

### Supervisor 实现

`_on_run_completed` 中 supervisor 优先于 goal。首次 `launch(session_id=xxx)` 创建监督者会话，后续 `launch(resume_session=xxx)` 复用。用户的监督标准作为 system_prompt，prompt 强调严格对照审查。

### Workspace 文件记忆

同一任务下所有 Agent 共享 workspace 目录，子 Agent 可直接读写父 Agent 产出的文件。任务文件持久化在 workspace 中，回退操作自动恢复文件到对应快照。

### Agent 规范

#### System Prompt

每种 Agent 启动时 OS 自动注入 system prompt，包含 workspace 路径、Agent 类型说明、可用工具：

```
## Available Tools

- Create sub-agents: use the Task tool (subagent_type=generative|interactive)
- report.py: `python .agent_os/report.py --result "<summary>"`
- send_message: use the SendMessage tool
```

#### 子 Agent 创建

父 Agent 通过 Task 工具创建子 Agent，OS 通过 PreToolUse Hook（`.agent_os/src/hooks/task_hook.py`）拦截并转发到 `/api/spawn`。参数通过 `SpawnTask` 模型定义：`prompt`、`type`、`step_id`、`goal`、`supervisor`。

#### 子 Agent 完成

子 Agent 调用 `report.py` 后 OS 自动检测。所有子 Agent 完成后，OS `--resume` 恢复父 Agent 并注入结果摘要。

### 会话回退

对话历史存储在 `~/.codebuddy/projects/<key>/<session_id>.jsonl`。回退时找到目标 seq → 截断文件 → 清空内存事件 → Git 回退 → Agent 下次 continue 读到截断历史，自然"忘记"。
