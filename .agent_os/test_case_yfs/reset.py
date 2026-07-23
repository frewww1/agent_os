#!/usr/bin/env python3
"""
一次性清理脚本 — 删 tweijieliu 代码 + svn revert 其他人的提交
"""

import shutil
import sys
import os
import subprocess
import re

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GAME_ROOT = PROJECT_ROOT  # g:\svn\trunk_cn\dragon\game

# ── tweijieliu 的部分：直接删除 ──
DELETE_FILES = [
    r"cross\comn_utils\activity_utils\usage_stage_reward_act_util.py",
    r"cross\comn_utils\activity_utils\yifanshang_act_util.py",
    r"cross\comn_utils\activity_utils\stock_util.py",
    r"cross\comn_utils\activity_utils\item_refund_act_util.py",
    r"cross\comn_utils\activity_utils\online_count_act_util.py",
    r"cross\comn_objects\global_stock_comp.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_usage_stage_reward_act.py",
    r"server\svr_objects\activity\comps\custom_comps\cross_stock.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_stock.py",
    r"server\svr_objects\activity\comps\custom_comps\worker_stock.py",
    r"server\svr_objects\activity\comps\custom_comps\cross_choice_gift.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_choice_gift.py",
    r"server\svr_objects\activity\comps\custom_comps\worker_choice_gift.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_item_refund_act.py",
    r"server\svr_objects\activity\comps\custom_comps\cross_online_count_act.py",
    r"server\svr_objects\activity\comps\custom_comps\global_online_count_act.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_online_count_act.py",
    r"server\svr_objects\activity\comps\custom_comps\worker_online_count_act.py",
    r"server\svr_objects\items\multi_choice_gift_item.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_usage_stage_reward_act.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_stock.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_item_refund_act.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_online_count_act.py",
    r"cross\comn_utils\activity_utils\checkers\__init__.py",
]

DELETE_DIRS = [
    r"client\ui\activity\yifanshang",
    r"cross\comn_objects\stock",
]

# ── 其他人提交的文件：svn revert ──
REVERT_FILES = [
    r"cross\sgr_data\AB\Activity@ActData.py",
    r"cross\sgr_data\AB\Custom@ActCompData.py",
    r"cross\sgr_data\AB\StageRewardData.py",
    r"cross\sgr_data\AB\UIPanelPrefabReflexData.py",
    r"cross\sgr_data\AB\UIPanelViewMappingData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\LangData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\MailData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\MarqueeData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\Activity@ActData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\common@ItemData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\gift@ItemData.py",
    r"cross\sgr_res_consts.py",
]


def run_svn(*args):
    """svn 命令，@ 文件名自动加末尾 @ 避免 peg revision 错误"""
    fixed = []
    for a in args:
        if "@" in a and not a.endswith("@"):
            a = a + "@"
        fixed.append(a)
    subprocess.run(["svn"] + fixed, cwd=PROJECT_ROOT)


def main():
    print("=" * 50)
    print("先 svn revert 其他人的文件...")
    for f in REVERT_FILES:
        src = os.path.join(GAME_ROOT, f)
        # revert 时会自动从仓库取回
        run_svn("revert", f)
        print(f"  revert: {f}")

    print("\n" + "=" * 50)
    print("再删除 tweijieliu 的代码文件...")
    for f in DELETE_FILES:
        src = os.path.join(GAME_ROOT, f)
        if os.path.exists(src):
            os.remove(src)
            print(f"  删除: {os.path.basename(f)}")
    for d in DELETE_DIRS:
        src = os.path.join(GAME_ROOT, d)
        if os.path.exists(src):
            shutil.rmtree(src)
            print(f"  删除目录: {d}")

    print(f"\nDone. 删除 {len(DELETE_FILES)} 文件 + {len(DELETE_DIRS)} 目录，revert {len(REVERT_FILES)} 文件。")


if __name__ == "__main__":
    main()
