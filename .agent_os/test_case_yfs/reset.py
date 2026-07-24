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
    # 一番赏
    r"cross\comn_utils\activity_utils\usage_stage_reward_act_util.py",
    r"cross\comn_utils\activity_utils\yifanshang_act_util.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_usage_stage_reward_act.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_usage_stage_reward_act.py",
    # 库存
    r"cross\comn_utils\activity_utils\stock_util.py",
    r"cross\comn_objects\global_stock_comp.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_stock.py",
    r"server\svr_objects\activity\comps\custom_comps\cross_stock.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_stock.py",
    r"server\svr_objects\activity\comps\custom_comps\worker_stock.py",
    # 自选礼包
    r"server\svr_objects\activity\comps\custom_comps\cross_choice_gift.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_choice_gift.py",
    r"server\svr_objects\activity\comps\custom_comps\worker_choice_gift.py",
    r"server\svr_objects\items\multi_choice_gift_item.py",
    # 退款
    r"cross\comn_utils\activity_utils\item_refund_act_util.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_item_refund_act.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_item_refund_act.py",
    # 在线人数
    r"cross\comn_utils\activity_utils\online_count_act_util.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_online_count_act.py",
    r"server\svr_objects\activity\comps\custom_comps\cross_online_count_act.py",
    r"server\svr_objects\activity\comps\custom_comps\global_online_count_act.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_online_count_act.py",
    r"server\svr_objects\activity\comps\custom_comps\worker_online_count_act.py",
    # 奖励池 reward_pool 全部删除
    r"cross\comn_utils\activity_utils\reward_pool_util.py",
    r"cross\comn_objects\reward_pool\checkers\__init__.py",
    r"cross\comn_objects\reward_pool\checkers\anti_guarantee.py",
    r"cross\comn_objects\reward_pool\checkers\non_replacement.py",
    r"cross\comn_objects\reward_pool\checkers\on_hit\__init__.py",
    r"cross\comn_objects\reward_pool\checkers\on_hit\global_broadcast.py",
    r"cross\comn_objects\reward_pool\checkers\on_hit\red_packet.py",
    r"cross\comn_objects\reward_pool\checkers\on_hit\roster_record.py",
    r"cross\comn_objects\reward_pool\reward_pool.py",
    r"server\svr_objects\activity\comps\custom_comps\cross_reward_pool.py",
    r"server\svr_objects\activity\comps\custom_comps\global_reward_pool.py",
    r"server\svr_objects\activity\comps\custom_comps\svr_reward_pool.py",
    r"server\svr_objects\activity\comps\custom_comps\worker_reward_pool.py",
    r"client\cli_objects\activity\comps\custom_comps\cli_reward_pool.py",
    r"cross\comn_utils\activity_utils\checkers\__init__.py",
]

DELETE_DIRS = [
    r"client\ui\activity\yifanshang",
    r"cross\comn_objects\stock",
    r"cross\comn_objects\reward_pool\checkers",
    r"cross\comn_utils\activity_utils\checkers",
]


def main():
    deleted = 0
    dirs_deleted = 0

    print("=" * 50)
    print("删除 7000856158 相关文件...")

    # 1. tweijieliu 代码文件
    for f in DELETE_FILES:
        src = os.path.join(GAME_ROOT, f)
        if os.path.exists(src):
            os.remove(src)
            print(f"  [文件] {os.path.basename(f)}")
            deleted += 1

    # 2. 目录
    for d in DELETE_DIRS:
        src = os.path.join(GAME_ROOT, d)
        if os.path.exists(src):
            shutil.rmtree(src)
            print(f"  [目录] {d}")
            dirs_deleted += 1

    # 3. 所有 CrossPoolData 配置文件（cross + server）
    import glob
    for pattern in [r"cross\sgr_data\**\*CrossPoolData*.py", r"server\**\*CrossPoolData*.py"]:
        for f in glob.glob(os.path.join(GAME_ROOT, pattern), recursive=True):
            rel = os.path.relpath(f, GAME_ROOT)
            os.remove(f)
            print(f"  [配置] {rel}")
            deleted += 1

    print(f"\nDone. 删除 {deleted} 文件 + {dirs_deleted} 目录。")


if __name__ == "__main__":
    main()
