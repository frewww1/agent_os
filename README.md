# Agent OS

多智能体任务编排系统 — 把 Claude Code 从单 agent 助手升级为可编排多 agent 协作的操作系统。

## 与直接用 Claude Code 的区别

| 能力 | Claude Code 原生 | Agent OS |
|------|:---:|:---:|
| 多 agent 并发协作 | ❌ | ✅ 主 agent spawn 子 agent 并行工作 |
| agent 间通信 | ❌ | ✅ 子 agent 向上汇报进度/结果，父 agent 自动恢复 |
| DAG 任务流水线 | ❌ | ✅ 定义多步骤工作流，拓扑排序，动态插入/重跑 |
| 持久记忆 | ❌ 对话结束即丢失 | ✅ Turn/Agent/Step 三级 git commit，可回溯回退 |
| 界面 | 终端 | ✅ Web 控制台 + VSCode 扩展 + 终端 |
| 任务审计 | ❌ | ✅ 每次文件变更都有 git 记录 |

## 启动

```bash
# 默认端口 8420
python .agent_os/main.py --cli codebuddy

# 或直接运行
start.bat
```

浏览器打开 http://127.0.0.1:8420

## 核心能力

### 多智能体协同

Agent 可在任务中动态 spawn 子 agent：

```bash
python .agent_os/spawn.py --tasks '[
  {"prompt": "审查后端代码", "agent_name": "Code Reviewer"},
  {"prompt": "检查前端安全", "agent_name": "Security Checker"}
]'
```

子 agent 通过 `send.py` 向上汇报进度，`report.py` 汇报最终结果。全部子 agent 完成后，父 agent 自动 resume。

### DAG 任务编排

定义多步骤工作流，管理依赖关系：

```bash
# 查看下一个可执行的 step
python .agent_os/dag.py --ready

# 查看所有 step 状态
python .agent_os/dag.py --status

# 动态插入新节点
python .agent_os/dag.py --add-step '{"id":"test","name":"测试","prompt":"...","depends_on":["dev"]}'

# 重跑某个 step 及下游
python .agent_os/dag.py --rerun <step_id>
```

### 三层 Git 记忆

每次交互自动打 git commit：
- **Turn 级** — 每轮对话
- **Agent 级** — 每个 agent 完成
- **Step 级** — DAG step 完成

```bash
# 查看某个任务的所有提交
git log --grep "<workspace_id>"
```

### Web 控制台

- Agent 树形可视化，父子关系一目了然
- 流式 Markdown 渲染 + 代码高亮
- 内联 diff（文件变更对比）
- 搜索、导出 Markdown/JSON
- 键盘快捷键（按 `?` 查看）

### VSCode 扩展

实时内联 diff，agent 编辑文件时直接在编辑器中显示增删行，支持 Accept/Reject。

## 依赖

- Python 3.8+
- Claude Code 或 CodeBuddy CLI
- Git

```bash
pip install -r .agent_os/requirements.txt
```
