# -*- coding: utf-8 -*-
"""强证据只在被测 App 内用，跳出去交模型。

## 这条规则的由来

用户原话：
    「只有本 APP 的才用强证据。跳出去都是让 AI 判断吧。
      虽然多一次访问。但更简单点。」

## 和它对称的两个失败方向

前一版错在**给外部界面加规则**：168 的 a3 写死 PhotoPicker 的 activity 名，
Android 17 换个分支就误报。我当时的补法（给"相册"列一串关键词）方向仍然错 ——
那还是拿规则去猜一个我们控制不了的界面。

正确的分界是**归属**（前台是不是被测 App），不是**内容**（长得像不像相册）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")


import android_agent.agent.nodes as nd  # noqa: E402

SUT = "com.zui.calendar"

print("=" * 70)
print("1) _is_sut：只认被测 App 自己")
print("=" * 70)

st = {"task": {"package": SUT}}

ck("被测 App 自己 -> True", nd._is_sut(st, SUT))
ck("★ 系统图库 -> False",
   not nd._is_sut(st, "com.android.providers.media.module"))
ck("★ 权限框 -> False",
   not nd._is_sut(st, "com.android.permissioncontroller"))
ck("桌面 -> False", not nd._is_sut(st, "com.zui.launcher"))
ck("★ 别的 App -> False", not nd._is_sut(st, "com.tencent.mm"))

# 包名要精确比，不能子串
ck("★ com.zui.calendar.widget 不算同一个 App（不能子串匹配）",
   not nd._is_sut(st, "com.zui.calendar.widget"))
ck("前缀相同的 com.zui.calendarx 也不算",
   not nd._is_sut(st, "com.zui.calendarx"))

print()
print("=" * 70)
print("2) 判不了归属时按『外部』处理（宁可多问一次）")
print("=" * 70)
ck("没拿到前台包名 -> False（不硬判）",
   not nd._is_sut(st, ""), "拿不到包名还硬判等于赌")
ck("用例没声明 package -> False",
   not nd._is_sut({"task": {}}, SUT))
ck("task 缺失也不炸", not nd._is_sut({}, SUT))

print()
print("=" * 70)
print("3) 强证据节点真的会因『不在本 App』而交模型")
print("=" * 70)
import inspect  # noqa: E402

src = inspect.getsource(nd.strong_target_node)
ck("★ 节点里有归属检查", "_is_sut(" in src)
ck("★ 不在本 App 时调 _ask_model", "_ask_model(" in src)
# 归属检查必须早于"找控件"—— 否则已经去解析 target 了
i_sut = src.index("_is_sut(")
i_resolve = src.index("_resolve_strong_target")
ck("★ 归属检查在找控件之前", i_sut < i_resolve,
   "顺序反了就等于先找目标再问归属，白做一次 xpath")

print()
print("=" * 70)
print("4) album 那套硬编码规则已经删干净")
print("=" * 70)
nodes_src = (ROOT / "src/android_agent/agent/nodes.py").read_text(encoding="utf-8")
for kw in ("album_reached", "_ALBUM_MARKERS", "_ALBUM_EXCLUDES", "album=any"):
    ck(f"已删除 {kw}", kw not in nodes_src)

print()
print("=" * 70)
print("5) 168 用例：跳出 App 的部分不给代码判据")
print("=" * 70)
from android_agent.cases.loader import load_case_by_name  # noqa: E402

spec, warns, ypath = load_case_by_name("168")
ck("168 加载成功", spec is not None, str(warns))

by_id = {s.id: s for s in spec.steps}
ck("★ s2 没有 landing 判据（终点在日历外面）",
   not by_id["s2"].landing, str(by_id["s2"].landing))

a3 = next(a for a in spec.asserts if a.id == "a3")
ck("★ a3 没有 check（交模型判）", not a3.check, str(a3.check))

# a1/a2 是 App 内部的，仍该保留代码判据
a1 = next(a for a in spec.asserts if a.id == "a1")
a2 = next(a for a in spec.asserts if a.id == "a2")
ck("a1 仍用 activity 判据（App 内部，稳定）",
   "activity" in (a1.check or {}), str(a1.check))
ck("a2 仍用 node_present 判据（App 内部，稳定）",
   "node_present" in (a2.check or {}), str(a2.check))

# 前面的步骤还在 App 内，应该还有 landing
ck("s0/s1 仍在 App 内，保留 landing",
   bool(by_id["s0"].landing) and bool(by_id["s1"].landing),
   f'{by_id["s0"].landing} / {by_id["s1"].landing}')

print()
print("=" * 70)
print("6) check 为 None 时判定层不炸、交回模型")
print("=" * 70)
v, why = nd.evaluate_check(None, state={"task": {"package": SUT}})
ck("check=None 返回 None（不是 False）", v is None, f"{v} / {why}")
ck("并说明是无判据", "无判据" in why, why)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
