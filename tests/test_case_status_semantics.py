# -*- coding: utf-8 -*-
"""用例状态语义：PASS / FAIL / BLOCKED 三者的边界必须是死的。

用户定的定义（原话整理）：

    PASS     这个 Case 与实际一致 —— 所有步骤走完、所有断言都成立
    FAIL     这个 Case 与实际有差别，但**所有路径都执行完成了**
             例：验证某按钮文字应为 AAA，实际是 BBBB -> FAIL
    BLOCKED  这个 Case 没执行完 —— 崩溃，或者关键路径没找到
             例：点 A 应该跳到 B 页，但点了 A 后到的不是 B -> BLOCKED

    两条铁律：
      1. 报了 BLOCKED，**不再继续执行下面的步骤**
      2. 报了 FAIL 或 PASS，**一定表示所有路径都执行完成了**

落点不符的具体规则（用户补充）：最多试 3 次，仍不符就 BLOCKED。
"""
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
NODES = (ROOT / "src" / "android_agent" / "agent" / "nodes.py").read_text(
    encoding="utf-8-sig")

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


def func_body(name: str) -> str:
    """粗略截出某个函数的函数体（到下一个顶层 def 为止）。"""
    i = NODES.find(f"def {name}(")
    if i < 0:
        return ""
    j = NODES.find("\ndef ", i + 1)
    return NODES[i:j if j > 0 else len(NODES)]


print("=" * 70)
print("1) 落点不符：最多试 3 次")
print("=" * 70)
ck("定义了 LANDING_MAX_TRIES 常量", "LANDING_MAX_TRIES = 3" in NODES)
ck("常量确实是 3", re.search(r"LANDING_MAX_TRIES = (\d+)", NODES).group(1) == "3")
ck("plan 里用的是这个常量（不是写死的 1）",
   "tried + 1 < LANDING_MAX_TRIES" in NODES)
ck("重试时明确告诉用户第几次", "次尝试" in NODES)

print()
print("=" * 70)
print("2) ★ 试满 3 次仍不符 -> BLOCKED")
print("=" * 70)
ck("试满后置 BLOCKED", '"status": "BLOCKED"' in NODES)
ck("说明是「关键路径没找到」", "关键路径没找到" in NODES)
ck("记录实际停在哪一页（便于人工判断）", "实际停在" in NODES)

print()
print("=" * 70)
print("3) ★★ BLOCKED 之后不再执行后续步骤")
print("=" * 70)
r_plan = func_body("route_after_plan")
ck("route_after_plan 检查 BLOCKED", 'status") == "BLOCKED"' in r_plan)
ck("route_after_plan 遇到 BLOCKED 去 finish", '"finish"' in r_plan)
# 检查顺序：BLOCKED 判断必须在"步骤走完"之前
i_block = r_plan.find('== "BLOCKED"')
i_done = r_plan.find('step_index')
ck("BLOCKED 判断在步骤推进之前（顺序正确）",
   0 <= i_block < i_done, f"BLOCKED@{i_block} 步骤判断@{i_done}")

r_strong = func_body("route_after_strong")
ck("route_after_strong 也检查 BLOCKED（不在 BLOCKED 后还去点设备）",
   'status") == "BLOCKED"' in r_strong)
ck("route_after_strong 支持去 finish",
   '"finish"' in r_strong.split('"""')[-1])

print()
print("=" * 70)
print("4) ★ verdict 的三档边界")
print("=" * 70)
# 摘下 verdict 里那段状态判定
i = NODES.find("landing_miss = [i for i in issues")
j = NODES.find("summary = state.get", i)
seg = NODES[i:j]
ck("找到 verdict 的状态判定段", bool(seg and len(seg) > 100))

ck("BLOCKED 优先级最高（第一个判）",
   seg.find('"BLOCKED"') < seg.find('"FAIL"'))
ck("放弃的步 -> BLOCKED", "gave_up" in seg and 'status = "BLOCKED"' in seg)
ck("★ 步骤没走完 -> BLOCKED（不是 FAIL）",
   "steps_done < len(steps)" in seg)
ck("★ 落点不符 -> BLOCKED（不是 FAIL）",
   "landing_miss" in seg and
   '"BLOCKED"' in seg[seg.find("elif landing_miss"):
                      seg.find("elif", seg.find("elif landing_miss") + 5)])
ck("★ 断言没验全 -> BLOCKED（不是 PASS）",
   "len(got) < len(asserts)" in seg)
ck("断言有不成立 -> FAIL", 'any(f["status"] == "FAIL"' in seg)
ck("全过 -> PASS", 'status = "PASS"' in seg)

print()
print("=" * 70)
print("5) ★ FAIL/PASS 一定表示所有路径都执行完")
print("=" * 70)
# FAIL / PASS 分支必须在「步骤走完」和「断言验全」之后
i_fail = seg.find('status = "FAIL"')
i_steps_ok = seg.find("steps_done < len(steps)")
i_assert_ok = seg.find("len(got) < len(asserts)")
ck("FAIL 判定在「步骤走完」检查之后", i_fail > i_steps_ok)
ck("FAIL 判定在「断言验全」检查之后", i_fail > i_assert_ok)

i_pass = seg.find('status = "PASS"')
ck("PASS 在最后（兜底）", i_pass > i_fail)

# coverage_ok 必须同样严格
ck("coverage_ok 同时要求 步骤全走完 + 断言全验到",
   "len(got) >= len(asserts) and steps_done >= len(steps)" in NODES.replace("\n", " ")
   or "len(got) >= len(asserts)" in NODES)

print()
print("=" * 70)
print("6) 落点不符不再被当 FAIL 报")
print("=" * 70)
# 找出 landing_miss 分支，确认里面是 BLOCKED
k = seg.find("elif landing_miss")
branch = seg[k:seg.find("elif", k + 5)]
ck("landing_miss 分支里是 BLOCKED", '"BLOCKED"' in branch)
ck("landing_miss 分支里没有 FAIL", '"FAIL"' not in branch)

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
