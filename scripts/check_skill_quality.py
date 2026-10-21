# -*- coding: utf-8 -*-
"""SKILL 质量检查：技能卡写得好不好，用可执行的规则判。

## 为什么需要它

`test_skill_card_consistency.py` 只回答"技能卡有没有教代码里不存在的东西"。
它管不了另一种失效：**教的东西是对的，但对读者没用**。

典型症状（都真实出现过）：
  · 失败考古 —— "以前模型连点 9 次、来回弹 58 轮"。读者是执行子 Agent，
    它读代码、不会重犯那些错误，这段不改变它任何一次动作。
  · 内部实现 —— "act → 读 next → act"。子 Agent 需要知道"代码会先试直点"，
    不需要知道状态机怎么转。
  · **无效约束** —— "不许写 python 脚本探测页面"。子 Agent 工具面里
    根本没有 shell，物理上写不了。写在卡里只是噪声，还占注意力。
  · 重复 —— 同一件事在两个文件/两节里各说一遍，模型可能读到两个版本。

## 判据（三条）

1. **可执行性**：每句话能映射成一个动作（点/按/说/不做什么）。
2. **必要性**：删掉这句，行为会不会变？不会 → 该删。
3. **一致性**：同一事实只有一处权威说法，且与代码一致。

这个脚本把能机械判的做掉；判不了的语义问题交给
`scripts/review_skill.py` 的评审子 Agent。

用法：
    python scripts/check_skill_quality.py            # 检查
    python scripts/check_skill_quality.py --verbose  # 列出每条明细
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SKILLS = Path.home() / ".agents" / "skills"
SKILL_DIR = SKILLS / "android-case-runner"
PLANNER_DIR = SKILLS / "android-case-planner"
# 子 Agent 真实拿到的工具面（见 preset 的 toolFilter.allow）
CHILD_TOOLS = {"android_case", "skill", "read", "glob", "grep"}

OK = 0
BAD = 0
WARN = 0


def ck(name, cond, detail="", warn_only=False):
    global OK, BAD, WARN
    if cond:
        OK += 1
        print(f"  ✅ {name}" + (f"  -> {detail}" if detail else ""))
    elif warn_only:
        WARN += 1
        print(f"  ⚠️  {name}" + (f"  -> {detail}" if detail else ""))
    else:
        BAD += 1
        print(f"  ❌ {name}" + (f"  -> {detail}" if detail else ""))


def read_all(d: Path) -> dict[str, str]:
    if not d.is_dir():
        return {}
    return {p.relative_to(d).as_posix(): p.read_text(encoding="utf-8-sig")
            for p in sorted(d.rglob("*.md"))}


def prose_lines(text: str):
    """产出 (行号, 行内容)，跳过代码块 / frontmatter / 空行。"""
    in_code = False
    in_fm = False
    for i, ln in enumerate(text.splitlines(), 1):
        s = ln.strip()
        if i == 1 and s == "---":
            in_fm = True
            continue
        if in_fm:
            if s == "---":
                in_fm = False
            continue
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s:
            continue
        yield i, s


files = read_all(SKILL_DIR)
if not files:
    print(f"找不到技能卡目录：{SKILL_DIR}")
    sys.exit(1)

main = files.get("SKILL.md", "")
corpus = "\n".join(files.values())
main_lines = len(main.splitlines())

print("=" * 72)
print("1) 体量：SKILL.md 是每次加载都进上下文的部分，必须克制")
print("=" * 72)
# 主卡超过 250 行基本意味着"把 references 该放的放进来了"
ck("SKILL.md 不超过 250 行", main_lines <= 250, f"{main_lines} 行")
ck("SKILL.md 不是空壳（细节别全推给 references）", main_lines >= 60, f"{main_lines} 行")
for name, txt in files.items():
    if name != "SKILL.md":
        n = len(txt.splitlines())
        ck(f"{name} 单文件不超过 150 行", n <= 150, f"{n} 行", warn_only=True)

print()
print("=" * 72)
print("2) ★ 失败考古：不许写以前模型怎么错（不改变当前行为）")
print("=" * 72)
# 这些词都在描述"过去发生过什么"。对读者（执行子 Agent）可能纯是噪声，
# 但也可能**夹带了可操作的规避写法**（"别把弹窗按钮当落点"→ 那该怎么写）。
#
# 所以判据不是"有无考古词"，而是：**含考古词的段落里有没有动作**。
# 有动作（必须/别/写/用/改成…）→ 结论可执行，保留，记警告。
# 只有故事、没有动作 → 纯考古，失败。
ARCHAEOLOGY = [
    r"早先", r"以前(?:的|模型)?", r"曾经", r"原先", r"过去",
    r"实测(?:会漏|踩到|发生)", r"踩过(?:的)?坑", r"真实案例",
    r"连点\s*\d+\s*次", r"来回弹\s*\d+\s*轮", r"烧掉\s*\d+\s*分钟",
]
ACTION_WORDS_ARCH = [
    "做", "给", "传", "用", "调", "写", "判", "报", "别", "不要",
    "必须", "不许", "改成", "走", "点", "按", "回", "填", "说", "读", "只",
]


def _has_action(text: str) -> bool:
    return any(w in text for w in ACTION_WORDS_ARCH)


pure = []      # 纯考古：只有故事没有动作 → 失败
mixed = []     # 考古 + 动作 → 可执行，仅提示
for name, txt in files.items():
    for ln, s in prose_lines(txt):
        if not any(re.search(pat, s) for pat in ARCHAEOLOGY):
            continue
        # 以"本节"为粒度判断有没有动作：往前找到所属小节标题
        lines = txt.splitlines()
        start = ln - 1
        while start > 0 and not lines[start - 1].lstrip().startswith("#"):
            start -= 1
        sec_end = ln
        while sec_end < len(lines) and not lines[sec_end].lstrip().startswith("#"):
            sec_end += 1
        sec_body = "\n".join(lines[start:sec_end])
        tag = f"{name}:{ln}  {s[:52]}"
        (mixed if _has_action(sec_body) else pure).append(tag)

ck("技能卡里没有纯失败考古（含考古词的段落必须带可操作结论）", not pure,
   f"{len(pure)} 处" if pure else "")
if pure:
    for h in pure[:12]:
        print(f"       ✗ {h}")
if mixed:
    print(f"       （{len(mixed)} 处考古夹带了可操作写法，保留："
          f"判据是「这段读完之后知道该怎么做」）")
    for h in mixed[:8]:
        print(f"       · {h}")

print()
print("=" * 72)
print("3) ★ 执行器内部实现：读者不需要知道状态机怎么转")
print("=" * 72)
# 子 Agent 需要知道的是**它该做什么**，不是代码内部怎么实现。
# `act → 读 next → act` 这类是维护者视角，写进卡里等于让模型去理解实现。
IMPLEMENTATION = [
    r"act\s*→\s*读\s*next", r"LangGraph", r"状态机", r"节点(?:执行)?",
    r"interrupt", r"answer_fn", r"网格|graph\.py|nodes\.py",
]
hits = []
for ln, s in prose_lines(main):
    for pat in IMPLEMENTATION:
        if re.search(pat, s):
            hits.append(f"SKILL.md:{ln}  {s[:56]}")
            break
ck("主卡不教执行器内部实现", not hits, f"{len(hits)} 处" if hits else "")
if hits:
    for h in hits[:8]:
        print(f"       {h}")

print()
print("=" * 72)
print("4) ★ 无效约束：约束的工具必须真的在工具面里")
print("=" * 72)
# 真实事故：卡里写着"不许写 python 脚本探测页面"，
# 而子 Agent 的工具面是 android_case/skill/read/glob/grep —— 没有 shell。
# 一条无法违反的禁令不是约束，是噪声，还占掉读者注意力。
forbidden_tools = re.findall(
    r"`(pwsh|bash|shell|write|edit|python|node|adb)`", corpus)
claimed = sorted(set(forbidden_tools))
# python/node/adb 是"语言/程序"，其余是工具名
fake = [t for t in claimed if t in {"pwsh", "bash", "shell", "write", "edit"}
        and t not in CHILD_TOOLS]
ck("没有引用子 Agent 拿不到的工具", not fake,
   f"引用了 {fake}（不在工具面 {sorted(CHILD_TOOLS)} 里）" if fake else "")

# 反例："不许写脚本"这类约束在无 shell 环境下不可能被违反
no_script = re.search(r"不许写.{0,6}脚本|不要写.{0,6}脚本|禁止写.{0,6}脚本", corpus)
ck("没有不许写脚本这类无效禁令（没有 shell 就写不了）", no_script is None,
   "删除该段" if no_script else "")

print()
print("=" * 72)
print("5) ★ 重复：同一件事只在一处权威说明")
print("=" * 72)
# 模型会读到两个略有出入的版本，然后照着其中一个做。
# 只查**小节标题级**的重复，正文里互相引用是正常的。
titles = {}
for name, txt in files.items():
    for ln, s in prose_lines(txt):
        if s.startswith("#"):
            t = re.sub(r"^#+\s*", "", s).strip()
            t = re.sub(r"[★⚠️\s]", "", t)
            if len(t) < 4:
                continue
            titles.setdefault(t, []).append(f"{name}:{ln}")
dupes = {k: v for k, v in titles.items() if len(v) > 1}
ck("没有重复的小节标题", not dupes,
   f"{list(dupes)[:3]}" if dupes else "")
if dupes:
    for k, v in list(dupes.items())[:6]:
        print(f"       「{k}」出现于 {v}")

print()
print("=" * 72)
print("6) 必备约束都在（删繁就简不能把纪律删掉）")
print("=" * 72)
REQUIRED = {
    "唯一命中门槛": r"唯一命中",
    "0/2 命中交模型": r"(0\s*个|2\s*个以上|2\s*个)",
    "没有判据≠已完成": r"没有判据.*不等于|不等于.*已完成",
    "按屏判断": r"按屏判断",
    "只有代码直点才回写": r"只有代码直点|代码直点.*回写",
    "权限只记账不进 YAML": r"不写进 YAML|不进 YAML",
    "稳定终态": r"稳定终态",
    "activity 子串匹配": r"子串",
    "判据不许改": r"不改判据|不许改|不要改.*判据",
    "BLOCKED 不等于功能有问题": r"BLOCKED.*(?:没跑完|不能|不是)",
    "steps_done=0 要明说": r"steps_done.*0|一步都没跑",
}
for label, pat in REQUIRED.items():
    ck(label, re.search(pat, corpus) is not None)

print()
print("=" * 72)
print("7) 每节都有动作（可执行性）")
print("=" * 72)
# 拆成小节，检查每节是否至少含一个"动作词"。
# 纯描述性的小节对执行者没用。
ACTION_WORDS = [
    "做", "给", "传", "用", "调", "写", "判", "报", "别", "不要",
    "必须", "不许", "改成", "走", "点", "按", "回", "填", "说", "读",
]
sections = re.split(r"\n(?=#{1,4}\s)", main)
weak = []
for sec in sections:
    body = sec.strip()
    if not body or body.startswith("---"):
        continue
    title = body.splitlines()[0].lstrip("# ").strip()
    if not title or title.startswith("#"):
        continue
    text = "\n".join(ln for _, ln in prose_lines(body))
    if len(text) < 40:
        continue              # 过短的小节不判
    if not any(w in text for w in ACTION_WORDS):
        weak.append(title[:40])
ck("每个有内容的小节都带动作", not weak,
   f"纯描述：{weak}" if weak else "", warn_only=True)

print()
print("=" * 72)
print(f"  {BAD} 失败 / {WARN} 警告 / {OK + BAD} 项")
print("=" * 72)
sys.exit(1 if BAD else 0)
