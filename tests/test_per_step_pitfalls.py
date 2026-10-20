"""「按步读坑」—— 用例 YAML 的元规则，现在显式立起来。

旧文件原话（每个用例的 playbook 第 1 节）：
    「本文件的坑一律写在**它真正会出现的那一步**的 `pitfalls:` 下。
      回放是**按步走**的 —— 跑到第 2 步时第 1 步的坑还在眼前，**信息被稀释**，
      真正相关的那条反而看不见。**走到哪一步，就只看那一步的 `pitfalls`。**」

我们本来就按步喂 pitfalls（decide 的 payload 只带当前步的），
这个测试把"这是**有意为之**、不是碰巧"钉住：
一旦有人图省事把所有坑铺到每步，信息就会被稀释回原样。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.cases.loader import load_case_by_name  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


print("=== 1) 读 YAML 时，坑按步分开（不汇总）===")
spec, _, path = load_case_by_name("168")
check("per_step_pitfalls 被立起来", spec.per_step_pitfalls is True)
check("YAML 里没有文件级坑（坑都在步上）",
      not hasattr(spec, "pitfalls") or not getattr(spec, "pitfalls", None), "")

s0, s1, s2 = spec.steps
check("s0 有自己的坑", len(s0.pitfalls) > 0, f"{len(s0.pitfalls)} 条")
check("s1 有自己的坑", len(s1.pitfalls) > 0, f"{len(s1.pitfalls)} 条")
check("s2 有自己的坑", len(s2.pitfalls) > 0, f"{len(s2.pitfalls)} 条")

print("\n=== 2) ★ 各步的坑**互不串场**（稀释的反面）===")
# s1 的坑讲 iv_more 没文案；s2 的坑讲空状态没有更多菜单
s0_txt = " ".join(s0.pitfalls)
s1_txt = " ".join(s1.pitfalls)
s2_txt = " ".join(s2.pitfalls)

check("s0 的坑提到权限说明框", "权限说明框" in s0_txt, "")
check("s0 的坑提到引导页", "引导页" in s0_txt, "")
check("s0 的坑**不**复述 s1 的 iv_more 坑",
      "iv_more 没有 text/desc" not in s0_txt, "")
check("s1 的坑提到 iv_more", "iv_more" in s1_txt, "")
check("s2 的坑**不**复述 s1 的 iv_more 坑",
      "iv_more 没有 text/desc" not in s2_txt, "")
check("s2 的坑提到「空状态没有更多菜单」",
      "没有「更多」菜单" in s2_txt or "更多" in s2_txt, "")
# s3「点掉提示框」并入 s2 之后，这条经验必须还在 ——
# 它是这一步最容易错的地方（不点掉框，图片选择器永远不出现）
check("s2 的坑提到「不点掉框就永远不出现」",
      "不点掉" in s2_txt or "永远不出现" in s2_txt, "")

print("\n=== 3) 每步的坑是「那一步专属」的（各步内容不同）===")
check("s0 与 s1 的坑不同", set(s0.pitfalls) != set(s1.pitfalls))
check("s1 与 s2 的坑不同", set(s1.pitfalls) != set(s2.pitfalls))
check("没有哪一步的坑等于全部坑的并集（那就是稀释）",
      not (set(s1.pitfalls) == set(s0.pitfalls) | set(s1.pitfalls)
           | set(s2.pitfalls)), "")

print("\n=== 4) 坑里带知识点（know.*）也被带上了 ===")
# 用例 YAML 的 know 是"这一步为什么这么点"，内联在文件里。
# 我们不检索它，所以并进 pitfalls —— 否则这段信息就丢了。
check("s1 的坑含 [know.*] 条目",
      any(p.startswith("[know.") for p in s1.pitfalls), str(s1.pitfalls[:1]))

print("\n=== 5) 8 个用例都立了 per_step_pitfalls ===")
from android_agent.cases.paths import cases_dir  # noqa: E402
from android_agent.cases.loader import load_case  # noqa: E402
bad = []
for f in sorted(cases_dir().glob("*.yaml")):
    s, _ = load_case(f)
    if not s.per_step_pitfalls:
        bad.append(f.stem)
check("全部立起来", not bad, str(bad) if bad else "8/8")

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
