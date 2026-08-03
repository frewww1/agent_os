#!/usr/bin/env python3
"""转发到 src/scripts/dag.py（根目录 shim）。"""
import os, runpy
_target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "scripts", "dag.py")
runpy.run_path(_target, run_name="__main__")
