"""Agent OS Dashboard — Pydantic request models."""

from typing import List

from pydantic import BaseModel, model_validator


class RunRequest(BaseModel):
    prompt: str
    agent_name: str | None = None
    model: str | None = None
    workspace_name: str | None = None
    system_prompt: str | None = None
    task_type: str = "generative"  # generative / interactive / explore
    interactive: bool = False
    goal: str | None = None
    max_goal_retries: int | None = None
    supervisor: str | None = None  # 监督 Agent 的 system prompt


class ContinueRequest(BaseModel):
    prompt: str
    model: str | None = None
    goal: str | None = None


class QualityPolicyRequest(BaseModel):
    goal: str | None = None
    max_retries: int = 5
    supervisor: str | None = None


class DagStartRequest(BaseModel):
    template_id: str
    workspace_name: str
    model: str | None = None
    resume: bool = False  # True = 基于现有 workspace 的 dag.json 继续，不重新初始化


class SpawnTask(BaseModel):
    prompt: str
    agent_name: str | None = None
    type: str = "generative"  # "generative" or "interactive"
    agent_type: str | None = None  # 兼容旧字段名
    subagent_type: str | None = None  # codebuddy Task 工具实际传的字段名
    model: str | None = None
    step_id: str | None = None  # DAG step 标识，OS 据此打 [step:<id>] commit
    goal: str | None = None  # DAG step 的 goal，透传给子 agent
    supervisor: str | None = None  # DAG step 的 supervisor，透传给子 agent

    @model_validator(mode="after")
    def resolve_type(self):
        """codebuddy Task 工具传 subagent_type，统一到 type 字段。"""
        if self.subagent_type:
            self.type = self.subagent_type
        elif self.agent_type:
            self.type = self.agent_type
        return self


class SpawnRequest(BaseModel):
    tasks: List[SpawnTask]
    wait_strategy: str = "all"
    parent_id: str = ""
    parent_session_id: str = ""


class LabelRequest(BaseModel):
    label: str


class PlanDecisionRequest(BaseModel):
    feedback: str = ""
    model: str | None = None


class ReportRequest(BaseModel):
    agent_id: str = ""
    result: str


class SendMsgRequest(BaseModel):
    agent_id: str = ""
    msg: str
