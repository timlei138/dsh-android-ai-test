# -*- coding: utf-8 -*-
"""死循环回归：模型反复报 done、check 不推进时，必须有出口。

真机实测两次（167，run #19/#20）：
    decide×80 / plan×79 / check×78 / act×1  -> BLOCKED（撞轮次上限）

成因：`round_in_step` 只在 act 节点里自增，模型光说 done 不点时它不动，
于是 `rounds >= limit` 永远不成立 —— 死循环没有出口。

这里用纯函数反复喂 check（模拟 plan 在同一步内保留计数），
断言它在有限轮内给出结论，而不是无限空转。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("ANDROID_SKILL_ROOT", str(ROOT / "assets"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.agent import nodes as nd

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


task = {
    "package": "com.zui.calendar",
    "steps": [
        {"id": "s0", "desc": "点掉引导框",
         "landing": "activity=.AllInOneActivity|TimetableGuideActivity"},
        {"id": "s1", "desc": "回首页", "landing": "activity=.AllInOneActivity"},
    ],
    "asserts": [],
}

# 落点**永远不满足**的情形：前台是一个既不是 AllInOne 也不是 Guide 的页面
# （比如模型说的完成其实没兑现）。这是最容易空转的场景。
state = {
    "task": task,
    "step_index": 0,
    "round_in_step": 0,          # ★ 关键：act 从没跑过，永远 0
    "max_rounds_per_step": 8,
    "last_activity": "com.zui.calendar.SomeOtherActivity",
    "last_package": "com.zui.calendar",
    "obs_clickable": [],
    "obs_texts": [],
    "obs_nodes_text": "",
    "action": {"step_done": True, "why": "模型认为做完了"},
    "retry_landing": 0,
    "fallback_reason": "",
    "issues": [],
    "findings": [],
}

print("=" * 70)
print("1) 反复报 done 但落点不符：必须在有限轮内收口")
print("=" * 70)

limit = 8
max_iter = 60
concluded = None
for i in range(max_iter):
    out = nd.step_check_node(state)
    if out.get("status") == "BLOCKED" or out.get("step_index") is not None:
        concluded = (i + 1, out)
        break
    # 模拟 plan 在同一步内的字段维护：清 issues、保留计数器、清 action
    same = True
    state = dict(state)
    state.update(out)
    state["issues"] = []
    state["action"] = {"step_done": True}
    if not same:
        state["check_rounds"] = 0

ck("★ check 在有限轮内给出了结论（没有空转 60 轮）", concluded is not None,
   f"跑满 {max_iter} 轮都没有结论")
if concluded:
    n, out = concluded
    print(f"       第 {n} 轮收口：status={out.get('status')} "
          f"step_index={out.get('step_index')}")
    ck("★ 收口是 BLOCKED（判不了就如实说，不假装完成）",
       out.get("status") == "BLOCKED", str(out.get("status")))
    ck("★ 收口原因写清楚了（落点不符要指名期望和实际）",
       any("落点" in str(x) for x in (out.get("issues") or [])),
       str(out.get("issues")))
    ck("★ 收口轮数不超过 LANDING_MAX_TRIES 太多",
       n <= nd.LANDING_MAX_TRIES + 1, f"{n} 轮")

print()
print("=" * 70)
print("2) act 正常也在跑时（round_in_step 会涨）同样收口")
print("=" * 70)

state2 = dict(state)
state2["check_rounds"] = 0
state2["retry_landing"] = 0
state2["findings"] = []
concluded2 = None
for i in range(max_iter):
    state2["round_in_step"] = i          # act 每轮都动过手
    out = nd.step_check_node(state2)
    if out.get("status") == "BLOCKED" or out.get("step_index") is not None:
        concluded2 = (i + 1, out)
        break
    state2 = dict(state2)
    state2.update(out)
    state2["issues"] = []
    state2["action"] = {"step_done": True}
ck("act 路线上也在有限轮内收口", concluded2 is not None, f"跑满 {max_iter} 轮")

print()
print("=" * 70)
print("3) 正常情形不能被误伤：落点命中就推进")
print("=" * 70)
ok = dict(state)
ok["check_rounds"] = 0
ok["retry_landing"] = 0
ok["findings"] = []
ok["last_activity"] = "com.zui.calendar.timetable.widget.TimetableGuideActivity"
ok["obs_clickable"] = [{"rid": "timetable_guide_button", "text": "我知道了",
                        "cls": "android.widget.Button"}]
out_ok = nd.step_check_node(ok)
ck("★★ 落点命中时正常推进到下一步（修复不能把正常路堵死）",
   out_ok.get("step_index") == 1 and not out_ok.get("status"),
   f"step_index={out_ok.get('step_index')} status={out_ok.get('status')}")

print()
print("=" * 70)
print("4) 换步时 check_rounds 归零（否则跨步累积会误杀）")
print("=" * 70)
nxt = dict(ok)
nxt["check_rounds"] = 5
nxt["step_index"] = 1
nxt["last_step_id"] = "s0"       # 和 s1 不同 -> same_step=False
nxt["prepare_wants"] = []
nxt["prep_applied"] = True
p = nd.plan_node(nxt)
ck("★★ 换步后 check_rounds 归零", p.get("check_rounds") == 0,
   f"check_rounds={p.get('check_rounds')}")

same = dict(nxt)
same["last_step_id"] = "s1"      # 同一步
same["fallback_screen"] = None
p2 = nd.plan_node(same)
ck("同一步内 check_rounds 保留（连续计数才有意义）",
   p2.get("check_rounds") == 5, f"check_rounds={p2.get('check_rounds')}")

print()
print("=" * 60)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 60)
sys.exit(1 if FAIL else 0)
