"""RunStateMachine — 基于 transitions 库的状态转换校验器。

用 transitions.Machine 的声明式转换表替代自研 frozenset。
不附加到 RunInfo（避免 pydantic 冲突），只做转换合法性校验 + 可视化。
线程安全（类级锁保护 set_state）。
"""
import threading

from transitions import Machine


class RunStateMachine:
    """agent 运行状态转换校验器。"""

    STATES = ['running', 'completed', 'failed', 'stopped', 'waiting', 'plan_pending']

    TRANSITIONS = [
        {'trigger': 'complete', 'source': 'running', 'dest': 'completed'},
        {'trigger': 'fail', 'source': 'running', 'dest': 'failed'},
        {'trigger': 'stop', 'source': 'running', 'dest': 'stopped'},
        {'trigger': 'wait', 'source': 'running', 'dest': 'waiting'},
        {'trigger': 'plan', 'source': 'running', 'dest': 'plan_pending'},
        {'trigger': 'resume', 'source': 'waiting', 'dest': 'running'},
        {'trigger': 'resume', 'source': 'plan_pending', 'dest': 'running'},
        {'trigger': 'resume', 'source': 'stopped', 'dest': 'running'},
        {'trigger': 'complete', 'source': 'waiting', 'dest': 'completed'},
        {'trigger': 'fail', 'source': 'waiting', 'dest': 'failed'},
        {'trigger': 'stop', 'source': 'plan_pending', 'dest': 'stopped'},
        {'trigger': 'stop', 'source': 'completed', 'dest': 'stopped'},
        {'trigger': 'stop', 'source': 'failed', 'dest': 'stopped'},
    ]

    # RunStatus value → trigger 映射
    _STATUS_TO_TRIGGER = {
        'completed': 'complete',
        'failed': 'fail',
        'stopped': 'stop',
        'waiting': 'wait',
        'plan_pending': 'plan',
        'running': 'resume',
    }

    _machine = None
    _lock = threading.Lock()

    @classmethod
    def _get_machine(cls) -> Machine:
        if cls._machine is None:
            cls._machine = Machine(
                states=cls.STATES,
                transitions=cls.TRANSITIONS,
                initial='running',
            )
        return cls._machine

    @classmethod
    def can_transition(cls, from_status: str, to_status: str) -> bool:
        """检查从 from_status 到 to_status 是否为合法转换。"""
        if from_status == to_status:
            return True
        trigger = cls._STATUS_TO_TRIGGER.get(to_status)
        if not trigger:
            return False
        with cls._lock:
            machine = cls._get_machine()
            try:
                machine.set_state(from_status)
            except ValueError:
                return False  # 未知状态
            may_method = getattr(machine, f'may_{trigger}', None)
            if may_method is None:
                return False
            return may_method()

    @classmethod
    def get_graph(cls):
        """返回状态图（需要 graphviz）。"""
        machine = cls._get_machine()
        return machine.get_graph()
