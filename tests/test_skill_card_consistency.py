# -*- coding: utf-8 -*-
"""技能卡与代码一致性：防止技能卡教模型用不存在的东西。

为什么单独有这个测试：
    技能卡在 `~/.agents/skills/` 下，**不在仓库里**。
    改代码时没人会想起它，于是它会慢慢腐化 ——
    实测就是这么发现的：代码里 `replay` 参数早删了，
    两份技能卡却还在教子 Agent 写 `android_case(replay=true)`、
    还在要求汇报不存在的 `replay_stats`。

    子 Agent 读的是技能卡、跑的是代码。两边不一致，
    它就会按技能卡写参数 —— 被 schema 拒绝，或者更糟：静默无效。

判据：技能卡里提到的参数/字段，必须在代码里真实存在。
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"D:\dsh\langgraph-poc")
SKILLS = Path.home() / ".agents" / "skills"
JS = (ROOT / "runner-tool" / "lib" / "index.js").read_text(encoding="utf-8")

OK = 0
BAD = 0


def ck(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        BAD += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


def read_skill(name: str) -> str | None:
    p = SKILLS / name / "SKILL.md"
    return p.read_text(encoding="utf-8-sig") if p.exists() else None


# ---- 代码实际认哪些参数 ------------------------------------------------
m = re.search(r"parameters:\s*\{(.*?)\n      \},", JS, re.S)
params = set(re.findall(r"^\s{8}(\w+):\s*\{", m.group(1), re.M)) if m else set()

print("=" * 70)
print("代码里 android_case 实际接受的参数")
print("=" * 70)
print("  " + ", ".join(sorted(params)))

print()
print("=" * 70)
print("1) 两份技能卡都存在")
print("=" * 70)
for name in ("android-case-runner", "android-case-planner"):
    txt = read_skill(name)
    ck(f"{name} 存在", txt is not None)
    if txt:
        globals()[f"TXT_{name}"] = txt

runner = read_skill("android-case-runner") or ""
planner = read_skill("android-case-planner") or ""

print()
print("=" * 70)
print("2) ★ 不许教 replay（参数已删除）")
print("=" * 70)


def bad_replay_calls(txt: str) -> list[str]:
    """挑出真正在教「传 replay 参数」的调用，跳过说明性文字。"""
    out = []
    for ln in txt.splitlines():
        s = ln.strip()
        # 说明"已删除/以前/不要传"的行不算
        if any(w in s for w in ("已删除", "以前", "不要", "没有 `replay`", "早已", "删除")):
            continue
        # 只认调用形态：android_case(...replay...)
        for call in re.findall(r"android_case\([^)]*\)", s):
            if re.search(r"\breplay\s*=", call):
                out.append(call)
    return out


for name, txt in (("runner", runner), ("planner", planner)):
    bad = bad_replay_calls(txt)
    ck(f"{name} 没教 replay= 参数", not bad, f"违规 {bad}")

print()
print("=" * 70)
print("3) ★ 不许提 replay_stats（报告字段不存在）")
print("=" * 70)
for name, txt in (("runner", runner), ("planner", planner)):
    hits = [
        ln.strip() for ln in txt.splitlines()
        if "replay_stats" in ln
        and not any(w in ln for w in ("不存在", "已删除", "以前", "删掉"))
    ]
    ck(f"{name} 没提 replay_stats", not hits, f"违规 {hits}")

print()
print("=" * 70)
print("4) ★ 技能卡提到的顶层参数必须真实存在")
print("=" * 70)
# ⚠️ 只检查 android_case(...) 的**顶层**关键字参数。
#    嵌套在 steps=[{...}] / asserts=[{...}] 里的 activity/rid/landing 等
#    是步骤对象和断言对象的字段，不是顶层参数 —— 之前把它们也算进来，
#    结果是误报（那几个字段本来就该那么写）。
#
#    做法：把 [ ... ] 平衡括号里的内容整体抹掉，只看最外层的 k=
def top_level_kwargs(txt: str) -> set[str]:
    found = set()
    for m0 in re.finditer(r"android_case\(", txt):
        i = m0.end()
        depth = 1
        buf = []
        while i < len(txt) and depth:
            ch = txt[i]
            if ch in "([{":
                depth += 1
                if depth == 1:
                    buf.append(ch)
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    break
                buf.append(ch)
            elif ch == '"':
                found_quote, i = _skip_str(txt, i)
                continue
            elif depth == 1:
                buf.append(ch)
            i += 1
        s = "".join(buf)
        found |= set(re.findall(r"(\w+)\s*=", s))
    return found


def _skip_str(txt, i):
    """跳过一段字符串字面量，返回 (内容, 结束位置)。"""
    q = txt[i]
    j = i + 1
    while j < len(txt) and txt[j] != q:
        j += 2 if txt[j] == "\\" else 1
    return txt[i:j + 1], j + 1


used = set()
for txt in (runner, planner):
    used |= top_level_kwargs(txt)
used -= {"title", "desc"}
unknown = sorted(u for u in used if u not in params)
ck("android_case 顶层参数都真实存在", not unknown,
   f"用到 {sorted(used)} / 未知 {unknown}")

# 反向：步骤/断言里该有的字段，别被这个检查顺手删掉
ck("步骤字段仍可自由使用（activity/rid/landing 等）",
   "landing" in (runner + planner) and "rid" in (runner + planner))

print()
print("=" * 70)
print("5) ★ 技能卡不许教 locate_seq / 存坐标（机制已删除）")
print("=" * 70)
for name, txt in (("runner", runner), ("planner", planner)):
    hits = [
        ln.strip()[:70] for ln in txt.splitlines()
        if "locate_seq" in ln and "不存在" not in ln and "已删" not in ln
    ]
    ck(f"{name} 没教 locate_seq", not hits, f"违规 {hits}")
    # 坐标：允许提「绝不存坐标」，不允许教具体坐标值
    badcoord = [
        ln.strip()[:70] for ln in txt.splitlines()
        if re.search(r"(next_x|next_y|coordinate|坐标)\s*[:=]\s*\d+", ln)
    ]
    ck(f"{name} 没教写死坐标", not badcoord, f"违规 {badcoord}")

print()
print("=" * 70)
print("6) runner 技能卡的关键规则都在")
print("=" * 70)
ck("说明自己是给执行子 Agent 的", "执行子 Agent" in runner)
ck("★ 强调唯一性门槛（0 或多个都不点）",
   "唯一命中" in runner and ("0 个" in runner or "2 个" in runner))
ck("★ 强调「没有判据 ≠ 已完成」",
   "没有判据" in runner and "不等于" in runner)
ck("★ 说明强证据直点是按屏判断（不是一次放弃）",
   "按屏判断" in runner)
ck("★ 说明权限只记账不进 YAML",
   "不写进 YAML" in runner or "不进 YAML" in runner)
ck("★ 说明只有代码直点才回写 YAML",
   "只有代码直点" in runner)
ck("说明落点必须是稳定终态", "稳定终态" in runner)
ck("说明 activity 短名子串匹配的坑", "子串" in runner)
ck("说明两个 venv 不能互换", "不能合并" in runner or "不能互换" in runner)

print()
print("=" * 70)
print("7) ★ config/ 快照与真实配置一致（防止改了原文件没同步）")
print("=" * 70)
import filecmp

SNAP = ROOT / "config"
PAIRS = [
    (Path.home() / ".dsh" / ".agent-presets" / "android-runner" / "agent.cordis.yml",
     SNAP / "preset" / "agent.cordis.yml"),
    (Path.home() / ".agents" / "skills" / "android-case-runner" / "SKILL.md",
     SNAP / "skills" / "android-case-runner.SKILL.md"),
    (Path.home() / ".agents" / "skills" / "android-case-planner" / "SKILL.md",
     SNAP / "skills" / "android-case-planner.SKILL.md"),
]
for live, snap in PAIRS:
    ck(f"{snap.name} 快照与真实一致",
       snap.exists() and filecmp.cmp(live, snap, shallow=False),
       "" if snap.exists() else "快照缺失，跑 python sync_config.py")

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
