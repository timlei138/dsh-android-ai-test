# -*- coding: utf-8 -*-
"""我们的 skill 卡内容检查（2026-10-27 起，独立于旧仓库）。

## 为什么只有内容检查、没有镜像检查了

原来还有一段"仓库镜像 config/skills/ 必须和 ~/.agents/skills/ 一致"。
那是旧仓库的做法 —— 技能卡在仓库里放一份快照，靠人记得同步。

**新方案不再有镜像**：技能卡的唯一真身在 `~/.agents/skills/`，
本工作区不复制一份。少一份副本 = 少一次不同步的机会。

## 检查什么

1. 关键内容还在（防止误删、防止被改写成别的意思）
2. 不再出现旧方案的痕迹（一出现，主 Agent 就会顺着去考古）
"""
import sys
from pathlib import Path

REAL = Path.home() / ".agents" / "skills"
CHECK = REAL / "android-case-check" / "SKILL.md"

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -> {detail}"
    print(line)


print("=" * 70)
print("0) 技能卡存在吗")
print("=" * 70)
ck("android-case-check 存在", CHECK.is_file(), str(CHECK))
if not CHECK.is_file():
    print()
    print(f"  {len(PASS)} 通过 / {len(FAIL)} 失败")
    sys.exit(1)

txt = CHECK.read_text(encoding="utf-8")

print()
print("=" * 70)
print("1) 关键内容还在")
print("=" * 70)
for key, desc in [
    ("有步骤吗", "检查项①：能不能说出先做什么再做什么"),
    ("有预期吗", "检查项②：做完该看到什么"),
    ("什么算 FAIL", "预期的判据是能不能说出反证"),
    ("android-executor", "派发目标 preset 名字"),
    ("PASS", "状态含义表"),
    ("BLOCKED", "状态含义表"),
]:
    ck(f"含「{desc}」", key in txt)

print()
print("=" * 70)
print("2) 不再出现旧方案的痕迹")
print("=" * 70)
# ⚠️ 用户原话：「langgraph-poc 这个都不用了。为啥还要查」
#    残留一个路径，主 Agent 就会顺着去翻整个仓库。
for bad in ("langgraph-poc", "_cases.json", "_learn.json", "android_case(",
            "run_case", "android-case-runner", "android-worker", "landing"):
    ck(f"不再提 `{bad}`", bad not in txt,
       "" if bad not in txt else "旧方案残留")

print()
print("=" * 70)
print(f"  {len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 70)

sys.exit(1 if FAIL else 0)
