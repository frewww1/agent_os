"""共享 fixtures 与测试环境引导。

统一 sys.path 与虚拟包注入，兼容所有测试的 import 模式：
  - 旧式直接 import：import dag_planner / import models（需 src/core 在 path）
  - 包导入：from agent_os.src.core.xxx import ...（需虚拟包 agent_os）
"""
import os
import sys
import types

import pytest

# ---- 路径 ----
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_OS_DIR = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
SRC_DIR = os.path.join(AGENT_OS_DIR, "src")
CORE_DIR = os.path.join(SRC_DIR, "core")

# 1. sys.path：兼容旧式直接 import（import dag_planner / import models 等）
for _p in (AGENT_OS_DIR, SRC_DIR, CORE_DIR, os.path.join(SRC_DIR, "mcp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 2. 虚拟包 agent_os：让 from agent_os.src.core.xxx 可用（只构建一次）
if "agent_os" not in sys.modules:
    _pkg = types.ModuleType("agent_os")
    _pkg.__path__ = [AGENT_OS_DIR]
    _pkg.__package__ = "agent_os"
    sys.modules["agent_os"] = _pkg

    _src_pkg = types.ModuleType("agent_os.src")
    _src_pkg.__path__ = [SRC_DIR]
    _src_pkg.__package__ = "agent_os.src"
    sys.modules["agent_os.src"] = _src_pkg

    # 注册子包（真正导入包模块，让 __init__.py 生效）
    import importlib
    for _sub in ("core", "mcp", "persistence", "scripts", "agent", "hooks"):
        _sub_path = os.path.join(SRC_DIR, _sub)
        if os.path.isdir(_sub_path):
            _full = f"agent_os.src.{_sub}"
            if _full not in sys.modules:
                try:
                    importlib.import_module(_full)
                except Exception:
                    # 回退：创建虚拟模块（兼容无 __init__.py 的目录）
                    _sub_mod = types.ModuleType(_full)
                    _sub_mod.__path__ = [_sub_path]
                    _sub_mod.__package__ = _full
                    sys.modules[_full] = _sub_mod


# ---- fixtures ----

@pytest.fixture
def simple_steps():
    """三节点线性 DAG: A -> B -> C"""
    return [
        {"id": "A", "name": "步骤A", "depends_on": [], "status": "pending"},
        {"id": "B", "name": "步骤B", "depends_on": ["A"], "status": "pending"},
        {"id": "C", "name": "步骤C", "depends_on": ["B"], "status": "pending"},
    ]


@pytest.fixture
def diamond_steps():
    """菱形 DAG: A -> B, A -> C, B -> D, C -> D"""
    return [
        {"id": "A", "depends_on": [], "status": "pending"},
        {"id": "B", "depends_on": ["A"], "status": "pending"},
        {"id": "C", "depends_on": ["A"], "status": "pending"},
        {"id": "D", "depends_on": ["B", "C"], "status": "pending"},
    ]


@pytest.fixture
def cyclic_steps():
    """有环 DAG: A -> B -> C -> A"""
    return [
        {"id": "A", "depends_on": ["C"], "status": "pending"},
        {"id": "B", "depends_on": ["A"], "status": "pending"},
        {"id": "C", "depends_on": ["B"], "status": "pending"},
    ]


@pytest.fixture
def backend():
    """test_agent_backend.py 的 backend fixture — 单元测试模式下 skip（需真实 CLI/SDK）。"""
    pytest.skip("backend fixture requires real CLI/SDK (E2E only)")
