"""RunStateMachine 单元测试 — 可单独运行：pytest tests/test_run_state_machine.py

直接测试 RunStateMachine.can_transition 类方法，不依赖 AgentOS。
"""
import pytest
from agent_os.src.core.infra.run_state_machine import RunStateMachine


class TestRunStateMachine:
    def test_valid_transition_running_to_completed(self):
        assert RunStateMachine.can_transition("running", "completed") is True

    def test_valid_transition_running_to_failed(self):
        assert RunStateMachine.can_transition("running", "failed") is True

    def test_valid_transition_waiting_to_running(self):
        assert RunStateMachine.can_transition("waiting", "running") is True

    def test_valid_transition_plan_pending_to_running(self):
        assert RunStateMachine.can_transition("plan_pending", "running") is True

    def test_valid_transition_stopped_to_running(self):
        assert RunStateMachine.can_transition("stopped", "running") is True

    def test_invalid_transition_completed_to_running(self):
        assert RunStateMachine.can_transition("completed", "running") is False

    def test_invalid_transition_failed_to_completed(self):
        assert RunStateMachine.can_transition("failed", "completed") is False

    def test_invalid_transition_stopped_to_completed(self):
        assert RunStateMachine.can_transition("stopped", "completed") is False

    def test_same_status(self):
        for s in RunStateMachine.STATES:
            assert RunStateMachine.can_transition(s, s) is True

    def test_all_valid_transitions(self):
        valid = [
            ("running", "completed"), ("running", "failed"), ("running", "stopped"),
            ("running", "waiting"), ("running", "plan_pending"),
            ("waiting", "running"), ("waiting", "completed"), ("waiting", "failed"),
            ("plan_pending", "running"), ("plan_pending", "stopped"),
            ("completed", "stopped"), ("failed", "stopped"), ("stopped", "running"),
        ]
        for from_s, to_s in valid:
            assert RunStateMachine.can_transition(from_s, to_s) is True, f"{from_s}->{to_s} should be valid"

    def test_invalid_transitions(self):
        invalid = [
            ("completed", "running"), ("completed", "waiting"), ("completed", "failed"),
            ("failed", "running"), ("failed", "completed"), ("failed", "waiting"),
            ("stopped", "completed"), ("stopped", "failed"), ("stopped", "waiting"),
            ("waiting", "stopped"), ("waiting", "plan_pending"),
            ("plan_pending", "completed"), ("plan_pending", "failed"), ("plan_pending", "waiting"),
        ]
        for from_s, to_s in invalid:
            assert RunStateMachine.can_transition(from_s, to_s) is False, f"{from_s}->{to_s} should be invalid"

    def test_unknown_status(self):
        assert RunStateMachine.can_transition("unknown", "running") is False
        assert RunStateMachine.can_transition("running", "unknown") is False
