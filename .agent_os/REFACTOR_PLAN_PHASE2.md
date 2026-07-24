# Agent OS 第二阶段重构计划：轻量库替代 + 测试补全 + 推理层 LangGraph

> 分支：`refactor/decouple-layers`（在第一阶段重构基础上继续）
> 第一阶段成果：EventBus / Registry / PromptBuilder / Orchestrator / transition() / CompletionSignal

## 一、轻量专用库替代自研（降维护成本，高匹配度）

用轻量库替代自研逻辑，而非用重框架。这些库的抽象和 agent_os 场景一对一匹配，无适配层。

### 1.1 状态机：`transitions` 库替代 `transition()` + `_VALID_TRANSITIONS`

**当前自研**：
```python
# process_manager.py
_VALID_TRANSITIONS = frozenset({
    (RunStatus.RUNNING, RunStatus.COMPLETED),
    (RunStatus.RUNNING, RunStatus.FAILED),
    ...
})

def _transition(self, run_info, to_status):
    pair = (run_info.status, to_status)
    if pair not in self._VALID_TRANSITIONS:
        logger.warning(...)
    run_info.status = to_status
    if to_status in (COMPLETED, FAILED, STOPPED):
        run_info.completed_at = datetime.now()
    self._mark_dirty()
```

**用 transitions 库**：
```python
from transitions import Machine

# 为每个 RunInfo 附加状态机
class RunStateMachine:
    states = ['running', 'completed', 'failed', 'stopped', 'waiting', 'plan_pending']
    transitions = [
        ['complete', 'running', 'completed'],
        ['fail', 'running', 'failed'],
        ['stop', 'running', 'stopped'],
        ['wait', 'running', 'waiting'],
        ['plan', 'running', 'plan_pending'],
        ['resume', 'waiting', 'running'],
        ['resume', 'plan_pending', 'running'],
        ['complete', 'waiting', 'completed'],
        ['fail', 'waiting', 'failed'],
        ['stop', 'plan_pending', 'stopped'],
        ['stop', 'completed', 'stopped'],
        ['stop', 'failed', 'stopped'],
        ['resume', 'stopped', 'running'],
    ]

    def __init__(self, run_info):
        self._ri = run_info
        self.machine = Machine(
            model=self, states=self.states, transitions=self.transitions,
            initial=run_info.status.value,
            after_state_change=self._on_change,
        )

    def _on_change(self):
        self._ri.status = RunStatus(self.state)
        if self.state in ('completed', 'failed', 'stopped'):
            self._ri.completed_at = datetime.now()
```

**收益**：
- 状态转换可视化（`machine.get_graph().draw()` 可画状态图）
- 转换校验内置（非法转换自动抛 `MachineError`）
- 状态查询内置（`is_running` / `may_complete` 等谓词）
- 社区维护，不自己管转换表

**成本**：`transitions` 库（纯 Python，零依赖，~50KB）

**落地步骤**：
1. `pip install transitions` 加到 requirements.txt
2. 创建 `src/core/run_state_machine.py`
3. RunInfo 创建时附加 RunStateMachine
4. `transition()` 改为调 `run_info._state_machine.complete()` / `.fail()` 等
5. 测试：验证状态转换行为不变

### 1.2 事件总线：评估 `blinker` 替代自研 `EventBus`

**当前自研**（`src/core/event_bus.py`，~70 行）：subscribe / publish / _dispatch

**用 blinker**：
```python
from blinker import signal

# 替代 EventBus
run_event = signal("run.event")
run_dirty = signal("run.dirty")
run_completion = signal("run.completion")

# 订阅
run_dirty.connect(lambda sender, **kw: pm._mark_dirty())
run_event.connect(lambda sender, **kw: pm._on_run_event(kw))

# 发布
run_event.send(run_id=run_info.run_id, event=event)
```

**收益**：
- 社区维护（Flask 生态组件，成熟稳定）
- 内置弱引用、断开连接、命名空间
- 零适配（API 和当前 EventBus 几乎一样）

**成本**：`blinker` 库（纯 Python，零依赖，~20KB）

**评估**：当前 EventBus 已经很简洁（70 行），替换收益有限。**建议保留自研 EventBus**，除非未来需要弱引用/断连等高级特性。

### 1.3 序列化：pydantic（已用，无需替换）

RunInfo / SpawnRequest 已用 pydantic BaseModel。CompletionSignal 用 dataclass。无需改动。

## 二、测试补全（当前最大维护风险）

### 2.1 现状

7 个预先存在的测试失败：

| 测试 | 失败原因 | 性质 |
|---|---|---|
| `test_root_system_prompt_contains_mcp_tools` | 断言 `os_spawn` in prompt，但 prompt 已改 | 测试与实现不同步 |
| `test_subagent_system_prompt_contains_mcp_tools` | 同上 | 测试与实现不同步 |
| `test_subagent_prompt_no_longer_differentiates_types` | 断言 `call report.py` not in prompt，但 prompt 已改 | 测试与实现不同步 |
| `test_root_agent_gets_mcp_config` | 断言 launch 收到 mcp_config，但 mock PM 缺配置 | mock 不完整 |
| `test_child_agent_gets_mcp_config` | 同上 | mock 不完整 |
| `test_continue_run_gets_mcp_config` | 同上 | mock 不完整 |
| `test_resume_prompt_contains_child_results` | 同上 | mock 不完整 |

另有多个测试因 import 路径过时无法收集（`test_agent_backend` / `test_sdk_backend` / `test_sdk_model`）。

### 2.2 修复计划

**P0：修复 3 个 prompt 断言不同步**（test_agent_lifecycle）
- 更新断言匹配当前 prompt 内容（`os_spawn` → `Task tool`，`call report.py` → `report.py is MANDATORY`）
- 或改为检查关键不变量而非精确字符串

**P1：修复 4 个 mcp_config mock 不完整**（test_spawn_children）
- mock PM 补 `pm._get_task_hook_config = MagicMock(return_value=None)` 或补完整 hook 配置
- 或更新测试断言匹配当前 mcp_config 传递逻辑

**P2：修复 import 路径过时**（test_agent_backend / test_sdk_backend / test_sdk_model）
- `from agent_os.src.backend import` → `from agent_os.src.agent.backend import`
- `from backend import` → `from agent_os.src.agent.backend import`

**P3：补 ProcessManager 死代码清理后的回归测试**
- 验证 `_resolve_process_exit` / `on_run_completed` 委托到 Orchestrator 后行为不变
- 验证 `transition()` 转换表覆盖所有合法路径

### 2.3 新增测试

- `test_event_bus.py`：EventBus 发布/订阅/线程安全
- `test_registry.py`：Registry 增删改查 + 树查询 + spawn 批次
- `test_prompt_builder.py`：PromptBuilder 纯函数输出
- `test_orchestrator.py`：Orchestrator resolve_process_exit + on_run_completed
- `test_transition.py`：状态转换表 + 非法转换校验

## 三、推理层 LangGraph（agent 内部多步推理编排）

### 3.1 适用场景

三个 in-process 多步推理循环，当前用 if/elif + return 实现，适合用 LangGraph 的 StateGraph 表达：

| 场景 | 当前实现 | LangGraph 收益 |
|---|---|---|
| Goal 评估循环 | `_on_run_completed` goal 分支 + `_evaluate_goal` | 显式 evaluate→feedback→evaluate 循环图 + checkpoint |
| Supervisor 审查循环 | `_on_run_completed` supervisor 分支 + `_spawn_supervisor` | 显式 review→PASS/CORRECTION 分支图 + checkpoint |
| DAG 调度决策 | 调度 agent 内部（OS 不参与） | **不适用**（决策在 agent 进程内，OS 不介入） |

### 3.2 Goal 评估循环

**当前**：
```python
# _on_run_completed 里
if run_info.goal and run_info.status == COMPLETED and retries < max:
    is_met, reason = self._evaluate_goal(run_info)
    if not is_met:
        self.continue_run(run_id, feedback)
        return  # 等 agent 再完成 → 再进 _on_run_completed → 再评估
```

**用 LangGraph**：
```python
# src/core/goal_graph.py
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Command

class GoalState(TypedDict):
    run_id: str
    goal: str
    retries: int
    max_retries: int
    is_met: bool
    eval_reason: str

class GoalGraph:
    """Goal 评估循环：评估→未达成→反馈重做→再评估。"""

    def __init__(self, pm):
        self._pm = pm
        self._graph = self._build()

    def _evaluate(self, state: GoalState) -> dict:
        """节点：调 codebuddy 子进程评估 goal。"""
        ri = self._pm.runs.get(state["run_id"])
        is_met, reason = self._pm._evaluate_goal(ri)
        return {"is_met": is_met, "eval_reason": reason}

    def _feedback(self, state: GoalState) -> Command:
        """节点：反馈 + 中断等 agent 重做完成。"""
        ri = self._pm.runs.get(state["run_id"])
        ri.add_text_line(f"[Agent OS] Goal not met: {state['eval_reason']}", kind="system")
        self._pm.continue_run(state["run_id"],
            f"Goal: {state['goal']}\nEvaluation: {state['eval_reason']}\nPlease fix.",
            source="os")
        # 中断：等 agent 再完成
        interrupt({"waiting_for": state["run_id"]})
        # agent 完成后，外部 resume → 回到 evaluate
        return Command(goto="evaluate", update={"retries": state["retries"] + 1})

    def _route(self, state: GoalState) -> str:
        if state["is_met"] or state["retries"] >= state["max_retries"]:
            return "done"
        return "feedback"

    def _build(self):
        g = StateGraph(GoalState)
        g.add_node("evaluate", self._evaluate)
        g.add_node("feedback", self._feedback)
        g.set_entry_point("evaluate")
        g.add_conditional_edges("evaluate", self._route, {
            "feedback": "feedback", "done": END,
        })
        g.add_edge("feedback", "evaluate")
        return g.compile(checkpointer=MemorySaver())

    def run(self, run_id, goal, max_retries=5):
        self._graph.invoke({
            "run_id": run_id, "goal": goal,
            "retries": 0, "max_retries": max_retries,
            "is_met": False, "eval_reason": "",
        }, config={"configurable": {"thread_id": run_id}})

    def resume(self, run_id):
        """agent 再次完成后调此方法恢复 graph。"""
        self._graph.invoke(None, config={"configurable": {"thread_id": run_id}})
```

**集成**：
```python
# Orchestrator.on_run_completed 里 goal 分支改为：
if run_info.goal and run_info.status == RunStatus.COMPLETED:
    if not run_info._goal_graph_active:
        run_info._goal_graph_active = True
        self._pm._goal_graph.run(run_info.run_id, run_info.goal, max_retries)
    else:
        self._pm._goal_graph.resume(run_info.run_id)
    return
```

**收益**：
- 循环图显式可视化（evaluate→feedback→evaluate）
- checkpoint 持久化 goal 状态（retries/reason），重启可恢复
- goal 逻辑从 _on_run_completed 的 if 分支独立出来，不和其他编排纠缠

**成本**：
- 依赖：`langgraph` + `langchain-core`（~10MB）
- interrupt/resume 桥接：约 20 行
- `_goal_graph_active` 标记管理

**风险**：
- LangGraph 的 interrupt 机制需要 agent 完成事件精确触发 resume
- 如果 agent 完成但 graph 没在等待（时序问题），需要处理
- langchain-core 版本升级可能 breaking

### 3.3 Supervisor 审查循环

**当前**：
```python
# _on_run_completed 里 supervisor 分支
if run_info.supervisor and run_info.status == COMPLETED:
    existing_sup = getattr(run_info, '_active_supervisor', None)
    if existing_sup:  # 已有 supervisor → resume 继续审查
        self.continue_run(existing_sup, feedback)
        return
    else:  # 首次创建 supervisor
        sup_run_id = self._spawn_supervisor(run_info)
        return
# supervisor 完成后 report.py PASS/CORRECTION → 触发 _on_run_completed(supervisor)
# CORRECTION → continue_run(agent, correction_feedback) → agent 重做 → 再审查
# PASS → agent 标记完成
```

**用 LangGraph**：
```python
# src/core/supervisor_graph.py
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, END
from langgraph.types import interrupt, Command

class SupervisorState(TypedDict):
    agent_run_id: str        # 被审查的 agent
    supervisor_run_id: str   # 审查 agent
    review_round: int
    verdict: Optional[str]   # "PASS" | "CORRECTION" | None
    correction_feedback: str

class SupervisorGraph:
    """Supervisor 审查循环：创建 supervisor → 等审查 → PASS/CORRECTION。"""

    def __init__(self, pm):
        self._pm = pm
        self._graph = self._build()

    def _spawn_supervisor(self, state: SupervisorState) -> dict:
        """节点：首次创建 supervisor agent。"""
        ri = self._pm.runs.get(state["agent_run_id"])
        sup_id = self._pm._spawn_supervisor(ri)
        return {"supervisor_run_id": sup_id}

    def _wait_review(self, state: SupervisorState) -> Command:
        """节点：中断等 supervisor 审查完成（PASS/CORRECTION）。"""
        verdict = interrupt({"waiting_for": state["supervisor_run_id"]})
        # supervisor report.py PASS/CORRECTION → 外部 resume 传入 verdict
        if verdict == "PASS":
            return Command(goto="pass", update={"verdict": "PASS"})
        else:
            return Command(goto="correct", update={"verdict": "CORRECTION", "correction_feedback": verdict})

    def _pass(self, state: SupervisorState) -> dict:
        """节点：supervisor PASS → agent 标记完成。"""
        ri = self._pm.runs.get(state["agent_run_id"])
        ri.add_text_line("[Agent OS] Supervisor: PASS — task complete", kind="system")
        # 标记 supervisor 完成，继续 agent 的 spawn resolution
        return {}

    def _correct(self, state: SupervisorState) -> Command:
        """节点：supervisor CORRECTION → 反馈 agent 重做 → 回到等审查。"""
        ri = self._pm.runs.get(state["agent_run_id"])
        self._pm.continue_run(state["agent_run_id"],
            f"Supervisor correction: {state['correction_feedback']}", source="os")
        # 中断等 agent 重做完成
        interrupt({"waiting_for": state["agent_run_id"]})
        # agent 重做完成 → resume supervisor 审查
        self._pm.continue_run(state["supervisor_run_id"],
            f"## Agent 新一轮产出\n\n请继续审查。", source="os")
        return Command(goto="wait_review", update={"review_round": state["review_round"] + 1})

    def _build(self):
        g = StateGraph(SupervisorState)
        g.add_node("spawn_sup", self._spawn_supervisor)
        g.add_node("wait_review", self._wait_review)
        g.add_node("pass", self._pass)
        g.add_node("correct", self._correct)
        g.set_entry_point("spawn_sup")
        g.add_edge("spawn_sup", "wait_review")
        # wait_review 内部用 Command 路由到 pass/correct
        g.add_edge("pass", END)
        g.add_edge("correct", "wait_review")  # 审查循环
        return g.compile(checkpointer=MemorySaver())

    def run(self, agent_run_id):
        self._graph.invoke({
            "agent_run_id": agent_run_id, "supervisor_run_id": "",
            "review_round": 0, "verdict": None, "correction_feedback": "",
        }, config={"configurable": {"thread_id": agent_run_id}})

    def resume(self, agent_run_id, verdict=None):
        """supervisor/agent 完成后调此方法恢复 graph。"""
        config = {"configurable": {"thread_id": agent_run_id}}
        self._graph.invoke(Command(resume=verdict) if verdict else None, config=config)
```

**收益**：
- 审查循环图显式（spawn_sup → wait_review → PASS/CORRECTION → correct → wait_review）
- 替代 _active_supervisor / _waiting_supervisor / _supervisor_done 三个幽灵字段
- checkpoint 持久化审查状态（review_round/verdict），重启可恢复
- supervisor 逻辑从 _on_run_completed 独立

**成本**：
- interrupt/resume 桥接比 goal 更复杂（两个中断点：等 supervisor + 等 agent 重做）
- 需要 report.py 的 PASS/CORRECTION 触发 graph resume
- 约 40 行桥接代码

**风险**：
- supervisor 和 agent 的完成时序更复杂（两个外部事件）
- _active_supervisor / _waiting_supervisor 的替换需要仔细对齐

### 3.4 DAG 调度决策：不适用

DAG 调度决策在**调度 agent 进程内部**（codebuddy 看 workspace + dag.json 决定下一步），OS 不参与决策。OS 只提供机制（dag.py / spawn.py），策略留给调度 agent。

LangGraph 编排的是 OS 进程内的决策，调度 agent 是外部进程——不适用。

### 3.5 落地优先级

| 场景 | 收益 | 成本 | 优先级 |
|---|---|---|---|
| Goal 评估循环 | 中（简化 retries 管理 + checkpoint） | 低（1 个中断点，~20 行桥接） | **P1** |
| Supervisor 审查循环 | 高（替代 3 个幽灵字段 + 复杂循环显式化） | 中（2 个中断点，~40 行桥接） | **P2** |
| DAG 调度决策 | 不适用 | — | 不做 |

### 3.6 依赖管理

```
# requirements.txt 新增
langgraph>=0.2.0
langchain-core>=0.3.0
```

langchain-core 是 langgraph 的唯一硬依赖，不拉 langchain 全家桶。

## 四、落地顺序

1. **测试补全（P0-P2）** — 修复 7 个预先存在失败 + 修 import 路径 + 补新模块测试
2. **transitions 库** — 替代 transition()，验证状态转换不变
3. **Goal 评估 LangGraph** — 最简单的推理循环试点，验证 interrupt/resume 桥接
4. **Supervisor 审查 LangGraph** — 更复杂的推理循环，替换幽灵字段
5. **评估 blinker** — 如果 EventBus 需要扩展（弱引用/断连），再考虑

## 五、风险与约束

- **不修改功能**：每个替换都靠测试验证行为不变
- **增量引入**：先试点一个场景（goal），验证后再扩展（supervisor）
- **可回退**：每个改动独立提交，出问题可回退到自研版本
- **依赖最小化**：只引 langgraph + langchain-core，不拉全家桶
