# -*- coding: utf-8 -*-
"""落点核对必须对**所有**步骤生效。

## 167 实数出来的 bug

s0 声明完成后停在课程表引导页（要求 AllInOneActivity），
日志里**一次都没重试**就进了 s1，轮04 直接接轮05：

    20:00:24 轮04 s0 act=...TimetableGuideActivity 落点=activity=.AllInOneActivity
    20:00:32      -> 判定本步完成
    20:00:32 轮05 s1 act=...TimetableGuideActivity       ← 没有重试

而 LANDING_MAX_TRIES = 3。

## 根因

重试分支的守卫写着：

    if done and issues and not state.get("fallback_reason"):

`fallback_reason` 的意思是「代码定不准，这步交给模型」—— 说的是**谁来点**。
它跟**点完到没到**是两件事，却被拿来决定要不要核对落点。

结果正是最不该的那个方向：越是代码没把握的步骤，越不核对落点。
而这类步骤恰恰最容易走偏。

这个测试直接调 step_check_node，两种 state 都必须重试。
光看源码里有没有那串字符是不够的 —— 断了行为，不看写法。
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("ANDROID_SKILL_ROOT", str(ROOT / "assets"))

import inspect  # noqa: E402

import android_agent.agent.nodes as nd  # noqa: E402

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


GUIDE = "com.zui.calendar.timetable.widget.TimetableGuideActivity"


def _state(**kw):
    base = {
        "task": {"case_id": "t", "package": "com.zui.calendar",
                 "steps": [{"id": "s0", "desc": "点权限框，落回日历首页",
                            "landing": "activity=.AllInOneActivity"}],
                 "asserts": [], "preconditions": []},
        "step_index": 0,
        "retry_landing": 0,
        "round_in_step": 0,
        "max_rounds_per_step": 6,
        "last_activity": GUIDE,
        "last_package": "com.zui.calendar",
        "obs_clickable": [{"rid": "timetable_guide_button",
                           "x": 1520, "y": 1521}],
        "obs_nodes_text": "",
        "findings": [], "issues": [], "progress": [],
        "fallback_reason": "",
        "seen_rids": [], "seen_texts": [], "locate_tried": [],
        "locate_attempts": 0, "max_locate_attempts": 3,
        "action": {"step_done": True, "why": "已在引导页"},
    }
    base.update(kw)
    return base


print("=" * 70)
print("1) 落点不符时，无论这步是不是'交给模型'，都必须重试")
print("=" * 70)

o1 = nd.step_check_node(_state(fallback_reason=""))
ck("代码直点步骤：留在本步重试",
   o1.get("step_index") is None and o1.get("retry_landing") == 1,
   f"step_index={o1.get('step_index')} retry={o1.get('retry_landing')}")

o2 = nd.step_check_node(_state(fallback_reason="当前屏可点项里没有 button1"))
ck("★ 交给模型的步骤：同样留在本步重试（167 就挂在这）",
   o2.get("step_index") is None and o2.get("retry_landing") == 1,
   f"step_index={o2.get('step_index')} —— None 才是重试，1 表示跳到下一步了")

ck("重试时如实记下落点不符",
   any("落点不符" in str(i) for i in (o2.get("issues") or [])))

print()
print("=" * 70)
print("2) 试满 LANDING_MAX_TRIES 后必须 BLOCKED，不能继续")
print("=" * 70)

o3 = nd.step_check_node(_state(
    fallback_reason="交给模型了",
    retry_landing=nd.LANDING_MAX_TRIES - 1))
ck("★ 试满 -> BLOCKED", o3.get("status") == "BLOCKED",
   f"status={o3.get('status')}")
ck("BLOCKED 时不再推进步骤", o3.get("step_index") is None,
   f"step_index={o3.get('step_index')}")

print()
print("=" * 70)
print("3) 落点相符时正常推进（别把重试写成死循环）")
print("=" * 70)

o4 = nd.step_check_node(_state(
    last_activity="com.zui.calendar.AllInOneActivity",
    fallback_reason="交给模型了"))
ck("落点相符 -> 推进到下一步", o4.get("step_index") == 1,
   f"step_index={o4.get('step_index')}")
ck("落点相符时不记问题", not o4.get("issues"),
   f"issues={o4.get('issues')}")

print()
print("=" * 70)
print("4) 系统权限框仍要单独处理（不能把 done 当成完成）")
print("=" * 70)

o5 = nd.step_check_node(_state(
    last_package="com.android.permissioncontroller",
    last_activity="com.android.permissioncontroller.permission.ui.GrantPermissionsActivity",
    fallback_reason="交给模型了"))
ck("系统权限框上声明完成 -> 不推进",
   o5.get("step_index") is None,
   f"step_index={o5.get('step_index')}")

print()
print("=" * 70)
print("5) 源码层面：那条守卫不该再挂回去")
print("=" * 70)

_src = inspect.getsource(nd)
_i = _src.find("if done and issues")
_line = _src[_i:].splitlines()[0] if _i >= 0 else ""
ck("★ 重试分支没有 fallback_reason 守卫",
   "fallback_reason" not in _line,
   f"当前写法：{_line.strip()}")
# 只看**判断语句**，不看注释。第一版用整段源码 count()，
# 结果把我在 L2998 写的那条解释性注释也数进去了 —— 又是一次"注释绊倒断言"。
# 路由里 `if not state.get("fallback_reason"): return strong_target`
# 是正当用途（交过模型就别再问一遍），与落点核对无关。
_guard_lines = [ln.strip() for ln in _src.splitlines()
                if 'and not state.get("fallback_reason")' in ln
                and not ln.strip().startswith("#")]
ck("没有任何 `and not fallback_reason` 守卫",
   not _guard_lines,
   f"还在：{_guard_lines}")

print()
print("=" * 70)
print("6) 167 的 s0 落点：授权流程本来就分岔，别把正常中间态当失败")
print("=" * 70)
# 实测三次真机，点「同意」之后的落点并不固定：
#     日历权限说明框 --同意--> 系统权限框 --允许--> 课程表引导页 / 日历首页
# 落到哪屏取决于还有几个权限要问。
# 判据只写 .AllInOneActivity 的话，落引导页就"落点不符"，重试 3 次 BLOCKED ——
# 而引导页是**正常中间态**。本步 pitfalls 里早就写了会遇到引导框，
# 判据却没跟着放宽，是 YAML 自己的矛盾。

from android_agent.cases.loader import load_case  # noqa: E402

_cases = ROOT / "assets" / "storage" / "cases"
_p167 = next(p for p in _cases.glob("*167*.yaml"))
_spec, _ = load_case(_p167)
_s0 = _spec.steps[0]
_land = str(_s0.landing or "")
_ok_guide, _ = nd.match_landing(
    _land, activity=GUIDE, clickable=[], state_pkg="com.zui.calendar")
_ok_home, _ = nd.match_landing(
    _land, activity="com.zui.calendar.AllInOneActivity",
    clickable=[], state_pkg="com.zui.calendar")
_ok_perm, _ = nd.match_landing(
    _land,
    activity="com.android.permissioncontroller.permission.ui.GrantPermissionsActivity",
    clickable=[], state_pkg="com.android.permissioncontroller")

ck("167 s0 落引导页算命中（实测的正常中间态）", _ok_guide,
   f"landing={_land}")
ck("167 s0 落日历首页算命中", _ok_home, f"landing={_land}")
ck("★ 但不许把系统权限框也当成落点", not _ok_perm,
   "权限框还没处理完，那不算到站")

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
