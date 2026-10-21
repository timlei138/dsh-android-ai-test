# -*- coding: utf-8 -*-
"""技能卡与代码一致性：防止技能卡教模型用不存在的东西。

为什么单独有这个测试：
    技能卡在 `~/.agents/skills/` 下，**不在仓库里**，而且和代码独立演化 ——
    代码改了没人会想起同步它们，于是技能卡会慢慢教出已经不存在的用法。

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
print("2) ★ 技能卡与 preset 里不许出现已废弃的词")
print("=" * 70)

# 这些词对应的机制已经从架构里彻底去掉：回放、按序重放、YAML 存坐标。
# 技能卡只要还提到它们（哪怕是"以前有过"），就会让读者以为它还活着，
# 而一个已经彻底删掉的概念，最干净的处理方式是**一个字都不留**。
RETIRED = {
    "replay": "回放机制已整体删除（坐标不可靠，改为每屏唯一命中直点）",
    "replay_stats": "报告无此字段，自动化率看 auto_rate",
    "locate_seq": "按序重放机制已删除",
}

for name, txt in (("runner", runner), ("planner", planner)):
    for word, why in RETIRED.items():
        hits = [ln.strip()[:60] for ln in txt.splitlines() if word in ln]
        ck(f"{name} 不含「{word}」", not hits, why if hits else "")

print()
print("=" * 70)
print("3) ★ preset 里也不许出现已废弃的词")
print("=" * 70)
preset_path = Path.home() / ".dsh" / ".agent-presets" / "android-runner" / "agent.cordis.yml"
preset_txt = preset_path.read_text(encoding="utf-8-sig") if preset_path.exists() else ""
for word in RETIRED:
    hits = [ln.strip()[:60] for ln in preset_txt.splitlines() if word in ln]
    ck(f"preset 不含「{word}」", not hits, f"违规 {hits}")

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


# 技能卡是 SKILL.md + references/ 两层结构，判据要看**整份技能卡** ——
# 只看 SKILL.md 会把"内容已经下沉到 references"误判成"内容丢了"。
def _skill_corpus(skill_name: str) -> str:
    base = SKILLS / skill_name
    if not base.is_dir():
        return ""
    return "\n".join(
        p.read_text(encoding="utf-8-sig")
        for p in sorted(base.rglob("*.md"))
    )


_corpus = _skill_corpus("android-case-runner") + _skill_corpus("android-case-planner")

ck("说明自己是给执行子 Agent 的", "执行子 Agent" in runner)
ck("★ 强调唯一性门槛（0 或多个都不点）",
   "唯一命中" in runner and ("0 个" in runner or "2 个" in runner))
ck("★ 强调「没有判据 ≠ 已完成」",
   "没有判据" in runner and "不等于" in runner)
ck("★ 说明强证据直点是按屏判断（不是一次放弃）",
   "按屏判断" in runner)
ck("★ 说明权限只记账不进 YAML",
   "不写进 YAML" in runner or "不进 YAML" in runner
   or "不写进 YAML" in _corpus or "不进 YAML" in _corpus)
ck("★ 说明只有代码直点才回写 YAML",
   "只有代码直点" in runner)
ck("说明落点必须是稳定终态", "稳定终态" in runner)
ck("说明 activity 短名子串匹配的坑", "子串" in runner)

# ── 运行环境口径（整份技能卡都要一致）──
# ⚠️ 这条以前断言"技能卡里写着两个 venv 不能互换"—— 那是**过时事实**。
#   两个 venv 早已合并成一个（rapidocr 支持 3.13 后），assets/.venv 已删。
#   旧断言把错误结论钉死成"正确"，技能卡照着它写等于持续教错。
#   现在反过来守：**整份技能卡里不许再出现分家的说法**。
_venv_words = ("两个 venv", "不能合并", "不能互换")
_hits = [ln.strip()[:70] for ln in _corpus.splitlines()
         if any(w in ln for w in _venv_words)]
ck("技能卡不再教「两个 venv」（已合并成一个）", not hits,
   f"违规 {_hits}" if _hits else "")
# 正面：必须说明只有一个运行环境，否则"缺依赖"会被归错因
ck("技能卡说明只有一个运行环境", "只有一个 venv" in _corpus)

# 老模块名 assets_path 早已改名为 android_agent.cases.paths —— 不许再教
_ap = [ln.strip()[:70] for ln in _corpus.splitlines()
       if "assets_path" in ln and "不存在" not in ln]
ck("技能卡不再提老模块名 assets_path", not _ap, f"违规 {_ap}" if _ap else "")

print()
print("=" * 70)
print("7) ★ config/ 快照与真实配置一致（防止改了原文件没同步）")
print("=" * 70)
import filecmp

SNAP = ROOT / "config"

# ★ 直接复用 sync_config 的清单，不在这里另抄一份。
#   以前两边各写一份 PAIRS，内容下沉到 references/ 之后两边一起漏了 ——
#   清单只有一处才不会漂移。
sys.path.insert(0, str(ROOT))
from sync_config import _pairs  # noqa: E402

_pa = _pairs()
ck("快照清单非空", bool(_pa), f"{len(_pa)} 个文件")
for live, snap in _pa:
    try:
        rel = snap.relative_to(ROOT)
    except ValueError:
        rel = snap
    ck(f"{rel} 快照与真实一致",
       snap.exists() and filecmp.cmp(live, snap, shallow=False),
       "" if snap.exists() else "快照缺失，跑 python sync_config.py")

# 反向：快照里不该有原文件已经删掉的东西（会变成骗人的"配置"）
_snap_known = {s for _, s in _pa}
_orphans = [p.relative_to(ROOT) for p in (SNAP / "skills").rglob("*.md")
            if p not in _snap_known]
ck("没有孤儿快照（原文件已删但快照还在）", not _orphans,
   f"违规 {_orphans}" if _orphans else "")

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
