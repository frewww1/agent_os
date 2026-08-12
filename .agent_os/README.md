# Agent OS（安装目录）

多智能体任务编排系统。在 AI Agent 之上提供多 agent 协同、DAG 流水线、会话管理。

> 完整用户文档见仓库根目录 `README.md`。本文是安装目录的补充说明。

## 快速开始

```bash
.agent_os\setup.bat   # 一键配置环境
.agent_os\start.bat   # 启动（端口被占用时自动递增，多开互不影响）
# 浏览器打开 http://127.0.0.1:8420
```

- 工作根默认为 CLI 运行目录（`--root` 可覆盖）；`state/`（agent 历史）与
  `workspaces/`（任务产物）落在工作根下，项目间互不干扰
- 启动时在工作根下创建 `.agent_os` junction 指向本安装目录（若工作根就是安装
  目录则跳过，避免自指）；Agent 提示词中的脚本命令注入**绝对路径**

## 配置

编辑 `cli_config.json`：

```json
{"cli": "codebuddy", "backend": "native"}                    // CLI 模式（默认）
{"cli": "codebuddy", "backend": "codebuddy-sdk"}             // SDK 模式
{"cli": "codebuddy", "backend": "native", "default_model": "deepseek-v4-flash-ioa"}
```

- `backend`：native（subprocess 启动 CLI）/ codebuddy-sdk
- `default_model`：默认模型（`--model` 参数 > 配置 > 内置兜底），Dashboard 可逐 agent 覆盖
- 重启生效

## 主要功能

- **多 Agent 协同**：主 Agent 可 spawn 子 Agent（`generative` / `interactive` / `explore`），
  共享 workspace，最多 3 层嵌套。子 Agent 通过 `report.py` 汇报完成，OS 自动 resume 父 Agent
  并只注入**本轮新完成**的子 Agent 结果（增量汇总）
- **DAG 任务流水线**：`dag.json` 声明步骤依赖，调度 Agent 按拓扑序执行。
  命令：`dag.py --ready` → `spawn.py` 派发 → 子 Agent 完成 → `dag.py --mark-done` 循环
- **Supervisor 监督**：带 supervisor 的 step 完成后 OS 启动审查 agent，PASS / CORRECTION
  自动驱动执行 agent 修正重试
- **Goal 评估**：step 可设置目标，完成后语义判断是否达成，未达成注入反馈重试
- **交互式 Agent**：等用户在 Dashboard 点 Done（被 Stop 后仍可 Done 完成）
- **超长会话动态加载**：滚动到顶部自动分页加载更早历史（从 DB + jsonl 持久化源读取，
  不受内存 1 万条事件上限裁剪影响）
- **SSE 实时推送**：断线自动重连并补齐积压事件；用户消息乐观渲染即时显示
- **OOM 兜底**：CLI 进程堆内存耗尽（code=134）自动 resume 同一会话（最多 3 次），
  启动时注入 `NODE_OPTIONS=--max-old-space-size=8192`

## 依赖

- Python 3.10+
- CodeBuddy CLI（或其他兼容 CLI）
- Git（仓库部署）

## 目录结构

```
.agent_os/
├── main.py               # 服务入口（端口自动分配）
├── start.bat / setup.bat # 启动 / 环境配置
├── cli_config.json       # cli / backend / default_model
├── dag.py / spawn.py / report.py / send.py   # Agent 可调用脚本（shim → src/scripts/）
├── dag_templates/        # DAG 模板（sgr_full_pipeline 等）
├── dashboard/            # FastAPI + 前端（templates / static / routers）
├── src/
│   ├── core/             # agent_os.py（调度核心）+ agents/（Agent 类型与提示词）
│   ├── agent/            # backend（Native/SDK）、stream_parser
│   ├── persistence/      # sqlite.py（DB 持久化）、session_parser.py
│   └── scripts/          # dag.py / spawn.py / report.py / send.py 实际实现
├── state/                # 运行数据（agents.db、models.json 等，不提交）
├── workspaces/           # 任务产物（工作根 = 安装目录时的默认位置）
└── tests/
```
