#!/usr/bin/env python3
"""DAG CLI — 调度 agent 操作 dag.json。用法参见 src/scripts/dag.py 文档。"""
import os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# 代理到 src/scripts/dag.py（内置 importlib 绕开 core.__init__）
_target = os.path.join(_HERE, "src", "scripts", "dag.py")
sys.path.insert(0, os.path.join(_HERE, "src"))
# exec 前把 __file__ 指向真实目标，避免 src/scripts/dag.py 基于 __file__
# 推导路径（dirname x2 + core/dag/planner.py）时偏一层导致 FileNotFoundError
_g = dict(globals())
_g["__file__"] = _target
exec(compile(open(_target, encoding="utf-8").read(), _target, "exec"), _g)
