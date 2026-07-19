"""共享 fixtures — DAG 测试用例所需的基础数据结构。"""
import pytest


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
