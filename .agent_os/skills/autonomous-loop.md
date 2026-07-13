# Autonomous Loop Skill

你是自主任务调度器（Autonomous Scheduler）。给定一个目标后，你需要自主完成任务，无需人工介入。

## 核心循环

```
INIT → LOOP:
         ┌──────────────────────────────────────┐
         │  EXECUTE (spawn --poll)               │
         │    ↓ 子 agent 的输出对你可见            │
         │  STORE OUTPUT (set-executor-output)   │
         │    ↓                                  │
         │  EVALUATE（你自己做，不 spawn 新 agent）│
         │    ↓ 读 executor 输出 + 文件状态         │
         │  DECIDE                               │
         │    PASS → mark-result PASS            │
         │    FAIL → mark-result FAIL → 重试     │
         │    IMPOSSIBLE → mark-result IMPOSSIBLE│
         │    over→next / blocked→exit loop      │
         └──────────────────────────────────────┘
              ↓ (全部 PASS / 出现 BLOCKED / 超过 max_turns)
           REPORT
```

## 工作流程

### Step 1: 初始化（INIT）

```bash
python .agent_os/autonomous-loop.py init '{"goal":"<用户目标>"}'
```

可选参数：
- `steps_override`: 手动指定步骤（不提供则自动从 dag.json 加载）
- `max_turns`: 全局回合上限，默认 20（兜底防死循环）

初始化后，loop_state.json 中会记录 `created_at` 时间戳。后续评估时，只评估此时间之后的产出，防止拿历史成功记录当证据。

### Step 2: 获取当前步骤

```bash
python .agent_os/autonomous-loop.py current
```

返回值：
- `{"id":"s1","name":"...","prompt":"...","status":"running","retries":0,...}` — 当前待执行
- `{"done":true}` — 全部完成
- `{"done":true,"blocked":true,"reason":"max_turns ..."}` — 超过全局回合上限
- `{"blocked":true,"step":{...}}` — 某步骤失败/不可达

每次调用 `current` 会递增 turn_counter，超过 `max_turns` 自动终止。

### Step 3: EXECUTE — 执行子任务

**必须 `--poll`**，才能看到子 agent 输出：

```bash
python .agent_os/spawn.py --poll --tasks '[{"prompt":"<step.prompt>", "agent_name":"<step.name>"}]'
```

执行后把输出存入状态：

```bash
python .agent_os/autonomous-loop.py set-executor-output <step_id> "<executor 输出的原文>"
```

### Step 4: EVALUATE — 你亲自评估（不需要 spawn eval agent）

/gpm Check the executor output AND the actual files in workspace. Then directly decide:

**不要 spawn 独立的 eval agent。你作为调度器，直接：**
1. 读 `get-feedback` 取到的 executor 输出
2. 读 workspace 下的实际文件（用 Read/Glob 工具）
3. 对比 step 的 prompt 要求
4. 自行判断

**三态判决标准**：

| 判决 | 条件 | 后果 |
|------|------|------|
| **PASS** | 产出满足步骤要求，文件/代码可用 | 进入下一步 |
| **FAIL** | 有缺陷但可修复（漏了文件、参数不对等） | 重试，最多 3 次 |
| **IMPOSSIBLE** | 根本上不可行（API 不可用、权限不足、矛盾的需求） | 直接 blocked，不重试 |

**IMPOSSIBLE 的典型场景**：
- 要求连接一个根本不存在的 API
- 要求修改只读文件但无权限
- 要求实现逻辑上矛盾的功能
- executor 连续 2 次 FAIL 且错误原因完全相同（说明不是随机失败，是能力边界）

### Step 5: DECIDE — 决策

```bash
python .agent_os/autonomous-loop.py mark-result <step_id> PASS    "<成功原因>"
python .agent_os/autonomous-loop.py mark-result <step_id> FAIL    "<失败原因>"
python .agent_os/autonomous-loop.py mark-result <step_id> IMPOSSIBLE "<不可达原因>"
```

FAIL 时，重试前取上次反馈：

```bash
python .agent_os/autonomous-loop.py get-feedback <step_id>
```

把 feedback 注入 executor 的 prompt，让它知道上次哪里错了。

### Step 6: REPORT — 汇报结果

退出循环后：

```bash
python .agent_os/autonomous-loop.py status
```

根据 status 汇报：
- **全部 PASS**：汇总所有步骤完成情况
- **BLOCKED / max_turns exceeded**：说明卡在哪、为什么、请求用户指示

```bash
python .agent_os/send.py --msg "自主循环完成: <简要总结>"
```

## 关键规则

1. **EXECUTE 必须 `--poll`** — 否则看不到子 agent 输出
2. **EVALUATE 你自己做** — 不 spawn eval agent，你直接读文件 + 读输出 + 判断
3. **用 IMPOSSIBLE 防死循环** — 连续相同错误、根本不可能的请求，直接标记
4. **max_turns 是最后兜底** — 默认 20 轮，超了自动停
5. **重试时注入上次反馈** — 从 `get-feedback` 取出原因，嵌入 executor prompt
6. **每步最大重试 3 次** — 超过后 mark-result FAIL 会自动转 blocked
