#!/usr/bin/env python3
"""DAG CLI — 调度 agent 操作 dag.json。用法参见 src/scripts/dag.py 文档。"""
import os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# 代理到 src/scripts/dag.py（内置 importlib 绕开 core.__init__）
_target = os.path.join(_HERE, "src", "scripts", "dag.py")
sys.path.insert(0, os.path.join(_HERE, "src"))
exec(open(_target, encoding="utf-8").read())
