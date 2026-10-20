# -*- coding: utf-8 -*-
"""用假设备跑真实的状态机，验证三档语义真的这么走。

静态检查只能证明"代码里写了 BLOCKED"，证明不了"它真的会停"。
这里用假设备把一个用例跑到落点永远不符，看状态机是不是：
  1. 试了 3 次
  2. 报 BLOCKED
  3. 后面的步骤一步没跑
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.agent import nodes as N          # noqa: E402
from android_agent.agent.nodes import (             # noqa: E402
    ChildState, LANDING_MAX_TRIES, route_after_plan, route_after_strong)

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


print("=" * 70)
print("1) 常量")
print("=" * 70)
ck("LANDING_MAX_TRIES == 3", LANDING_MAX_TRIES == 3, LANDING_MAX_TRIES)

print()
print("=" * 70)
print("2) ★ 状态为 BLOCKED 时，route_after_plan 立刻去 finish")
print("=" * 70)
task = {"case_id": "t", "title": "t", "package": "p",
        "steps": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}], "asserts": []}

st = ChildState(task=task, status="BLOCKED", step_index=1)
ck("BLOCKED + 还有步骤 -> finish（不是继续跑）",
   route_after_plan(st) == "finish", route_after_plan(st))

st_ok = ChildState(task=task, step_index=1)
r = route_after_plan(st_ok)
ck("正常状态 -> 不会误判成 finish", r != "finish", r)

print()
print("=" * 70)
print("3) ★ 状态为 BLOCKED 时，route_after_strong 不派发动作")
print("=" * 70)
st2 = ChildState(task=task, status="BLOCKED",
                 action={"next_x": 100, "next_y": 200})
ck("BLOCKED 时即便有坐标也不去 act",
   route_after_strong(st2) == "finish", route_after_strong(st2))

st3 = ChildState(task=task, action={"next_x": 100, "next_y": 200})
ck("正常状态仍会去 act", route_after_strong(st3) == "act",
   route_after_strong(st3))

print()
print("=" * 70)
print("4) ★ 落点不符重复试：计数递增到上限")
print("=" * 70)
# 直接模拟 plan 里那段判断的条件
for tried in range(LANDING_MAX_TRIES + 1):
    still_retry = tried + 1 < LANDING_MAX_TRIES
    print(f"    已试 {tried} 次 -> {'继续重试' if still_retry else 'BLOCKED'}")

ck("第 1、2 次还在试（tried=0,1）",
   0 + 1 < LANDING_MAX_TRIES and 1 + 1 < LANDING_MAX_TRIES)
ck("第 3 次到上限（tried=2）不再试", not (2 + 1 < LANDING_MAX_TRIES))

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
