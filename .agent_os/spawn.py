#!/usr/bin/env python3
"""转发到 src/scripts/spawn.py（根目录 shim，保持 agent 调用路径兼容）。"""
import os, runpy
_target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "scripts", "spawn.py")
runpy.run_path(_target, run_name="__main__")
