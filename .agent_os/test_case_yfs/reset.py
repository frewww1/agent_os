#!/usr/bin/env python3
"""
一次性清理脚本 — 删 tweijieliu 代码 + svn revert 其他人的提交
"""

import shutil
import sys
import os
import subprocess
import re
import glob

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
    r"client\ui\yifanshang",
    r"cross\comn_objects\stock",
    r"cross\comn_objects\reward_pool\checkers",
    r"cross\comn_utils\activity_utils\checkers",
]

# ── 其他人修改的文件：svn revert ──
REVERT_FILES = [
    # cross/sgr_data AB 数据源
    r"cross\sgr_data\AB\Activity@ActData.py",
    r"cross\sgr_data\AB\Custom@ActCompData.py",
    r"cross\sgr_data\AB\StageRewardData.py",
    r"cross\sgr_data\AB\UIPanelPrefabReflexData.py",
    r"cross\sgr_data\AB\UIPanelViewMappingData.py",
    r"cross\sgr_data\AB\all_design_data_names.py",
    r"cross\sgr_data\AB\bruce@UIPanelData.py",
    # cross/sgr_data lang
    r"cross\sgr_data\lang\zh-Hans\AB\LangData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\MailData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\MarqueeData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\Activity@ActData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\common@ItemData.py",
    r"cross\sgr_data\lang\zh-Hans\AB\gift@ItemData.py",
    # cross 其他
    r"cross\sgr_res_consts.py",
    r"cross\comn_utils\plant_utils.py",
    # client UI
    r"client\ui\shop\pay_shop_main_face\main_view.py",
    r"client\ui\shop\pay_shop_main_face\main_model.py",
    r"client\ui\shop\pay_shop_main_face\main_control.py",
    r"client\cli_visuals\sandbox\shop_visual.py",
]


def run_svn(*args):
    """svn 命令，@ 文件名自动加末尾 @ 避免 peg revision 错误"""
    fixed = []
    for a in args:
        if "@" in a and not a.endswith("@"):
            a = a + "@"
        fixed.append(a)
    subprocess.run(["svn"] + fixed, cwd=PROJECT_ROOT)


def find_and_revert_lang_design_names(root_dir):
    """svn revert 所有 lang 下的 all_design_data_names.py"""
    count = 0
    for dp, dn, fn in os.walk(root_dir):
        dn[:] = [d for d in dn if d not in ('.svn', '__pycache__')]
        for f in fn:
            if f == 'all_design_data_names.py':
                rel = os.path.relpath(os.path.join(dp, f), GAME_ROOT)
                run_svn("revert", rel)
                print(f"  revert: {rel}")
                count += 1
    return count


def find_and_delete_hanging_crosspool():
    """删除可能遗漏的 CrossPoolData 文件（server/finalized_na 等 glob 没覆盖到的）"""
    count = 0
    for dp, dn, fn in os.walk(GAME_ROOT):
        dn[:] = [d for d in dn if d not in ('.svn', '__pycache__')]
        for f in fn:
            if 'CrossPoolData' in f and f.endswith('.py'):
                fp = os.path.join(dp, f)
                rel = os.path.relpath(fp, GAME_ROOT)
                if os.path.exists(fp):
                    os.remove(fp)
                    print(f"  删除遗漏: {rel}")
                    count += 1
    return count


def main():
    deleted = 0
    dirs_deleted = 0

    print("=" * 60)
    print("1. svn revert 其他人修改的文件...")
    print("=" * 60)
    for f in REVERT_FILES:
        run_svn("revert", f)
        print(f"  revert: {f}")

    # revert 所有 lang 下的 all_design_data_names.py
    lang_dir = os.path.join(GAME_ROOT, r"cross\sgr_data\lang")
    if os.path.isdir(lang_dir):
        reverted = find_and_revert_lang_design_names(lang_dir)
        print(f"  (lang all_design_data_names: {reverted} 个)")

    print("\n" + "=" * 60)
    print("2. 删除 tweijieliu 代码文件...")
    print("=" * 60)
    for f in DELETE_FILES:
        src = os.path.join(GAME_ROOT, f)
        if os.path.exists(src):
            os.remove(src)
            print(f"  删除: {f}")
            deleted += 1

    for d in DELETE_DIRS:
        src = os.path.join(GAME_ROOT, d)
        if os.path.exists(src):
            shutil.rmtree(src)
            print(f"  删除目录: {d}")
            dirs_deleted += 1

    print("\n" + "=" * 60)
    print("3. 删除所有 CrossPoolData 配置表...")
    print("=" * 60)
    imported_glob = glob.glob
    for pattern in [r"cross\sgr_data\**\*CrossPoolData*.py", r"server\**\*CrossPoolData*.py"]:
        for f in imported_glob(os.path.join(GAME_ROOT, pattern), recursive=True):
            rel = os.path.relpath(f, GAME_ROOT)
            os.remove(f)
            print(f"  删除: {rel}")
            deleted += 1

    # 兜底遍历删除可能遗漏的 CrossPoolData
    hanging = find_and_delete_hanging_crosspool()
    if hanging:
        print(f"  (遗漏兜底: {hanging} 个)")

    print("\n" + "=" * 60)
    print("4. 清理 cross/ 和 client/ 中残留的 yifanshang 配置表...")
    print("=" * 60)
    yish_residue = 0
    residue_dirs = [
        r"cross\sgr_data\AB",
        r"cross\sgr_data\lang",
        r"client\ui",
        r"client\cli_visuals",
    ]
    yish_kws = ("yifanshang", "reward_pool", "2609DL", "YIFANSHANG", "RewardPool")
    for rd in residue_dirs:
        root_dir = os.path.join(GAME_ROOT, rd)
        if not os.path.isdir(root_dir):
            continue
        for dp, dn, fn in os.walk(root_dir):
            dn[:] = [d for d in dn if d not in (".svn", "__pycache__")]
            for f in fn:
                if not f.endswith(".py"):
                    continue
                fp = os.path.join(dp, f)
                rel = os.path.relpath(fp, GAME_ROOT)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()
                    if any(kw in content for kw in yish_kws):
                        os.remove(fp)
                        print(f"  删除: {rel}")
                        yish_residue += 1
                except Exception:
                    pass

    print("\n" + "=" * 60)
    print("5. 清理 cross/sgr_res_consts.py 等代码文件中的 yifanshang 引用...")
    print("=" * 60)
    yish_clean = 0
    code_files = [
        r"cross\sgr_res_consts.py",
        r"cross\comn_utils\plant_utils.py",
    ]
    for cf in code_files:
        fp = os.path.join(GAME_ROOT, cf)
        if not os.path.exists(fp):
            continue
        with open(fp, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        new_lines = []
        for line in lines:
            lowered = line.lower()
            if any(kw in lowered for kw in ("yifanshang", "2609dl")):
                continue
            new_lines.append(line)
        if len(new_lines) != len(lines):
            with open(fp, "w", encoding="utf-8") as fh:
                fh.writelines(new_lines)
            print(f"  清理: {cf} ({len(lines) - len(new_lines)} 行)")
            yish_clean += 1

    print(f"\nDone. revert {len(REVERT_FILES)} 文件, "
          f"删除 {deleted} 文件 + {dirs_deleted} 目录 + "
          f"{yish_residue} 残留配置表, 清理 {yish_clean} 代码文件。")


if __name__ == "__main__":
    main()
