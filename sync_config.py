# -*- coding: utf-8 -*-
"""把仓库外的关键配置（子 Agent preset / 技能卡）同步进 config/ 快照。

为什么要这份快照：
    子 Agent 的行为由两个**仓库外**的文件决定：
      · ~/.dsh/.agent-presets/android-runner/agent.cordis.yml  → 工具面
      · ~/.agents/skills/android-case-*/SKILL.md              → 技能卡
    它们决定"子 Agent 能干什么、被教成什么样"，但不在 git 里，
    改了没记录、坏了没法回退。而且它们是**独立演化**的 ——
    代码改了没人会想起同步它们，于是会慢慢教出错误的用法。

    config/ 下是**镜像**（可读、可 diff、可回退），
    真正生效的永远是原路径。改完原文件跑一次本脚本同步。

用法：
    python sync_config.py          # 原文件 -> config/ 快照
    python sync_config.py --check  # 只比对，不一致就退出码 1（给测试用）
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 仓库根从本文件位置推，不写死盘符 —— 换机器/换目录都能跑
ROOT = Path(__file__).resolve().parent
SNAP = ROOT / "config"
HOME = Path.home()

PAIRS = [
    (HOME / ".dsh" / ".agent-presets" / "android-runner" / "agent.cordis.yml",
     SNAP / "preset" / "agent.cordis.yml"),
    (HOME / ".agents" / "skills" / "android-case-runner" / "SKILL.md",
     SNAP / "skills" / "android-case-runner.SKILL.md"),
    (HOME / ".agents" / "skills" / "android-case-planner" / "SKILL.md",
     SNAP / "skills" / "android-case-planner.SKILL.md"),
]


def check_only() -> int:
    bad = 0
    for live, snap in PAIRS:
        if not live.exists():
            print(f"  ⚠️  原文件不存在：{live}")
            bad += 1
            continue
        if not snap.exists():
            print(f"  ❌ 快照缺失：{snap.name}")
            bad += 1
            continue
        if filecmp.cmp(live, snap, shallow=False):
            print(f"  ✅ 一致：{snap.name}")
        else:
            print(f"  ❌ 不一致：{snap.name}")
            print(f"      原文件 {live}")
            print(f"      跑 `python sync_config.py` 同步")
            bad += 1
    return 1 if bad else 0


def sync() -> int:
    for live, snap in PAIRS:
        if not live.exists():
            print(f"  ⚠️  原文件不存在，跳过：{live}")
            continue
        snap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live, snap)
        print(f"  · {snap.relative_to(ROOT)}  <-  {live}")
    print("  同步完成")
    return 0


if __name__ == "__main__":
    print("=" * 60)
    if "--check" in sys.argv:
        print("比对 config/ 快照与真实配置")
        print("=" * 60)
        sys.exit(check_only())
    print("同步 config/ 快照")
    print("=" * 60)
    sys.exit(sync())
