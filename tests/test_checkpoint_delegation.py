"""落点判定必须用工具层那一份，不能再自己写一套。

## 为什么单独立这个测试

这次重构的核心是「不重复造轮子」：`match_landing` / `_pathmap_expect`
全部改成委托给 `assets/tools/checkpoint.py` 和 `pathmap.py`。

但"委托"这种事**很容易悄悄退回去** —— 以后谁顺手加个
`if key == "activity": return want in activity`，就又变回手写版了，
而且测试照样全绿（因为简单用例手写版也能过）。

所以这里专门测**手写版会错、工具层才对**的那些边界：

  1. `EditTimetableActivity` 不能被 `activity=TimetableActivity` 命中。
     手写版用 `endswith`，必然误命中 —— 检查点在**错误的页面**上
     静默通过，这比不校验更危险。
  2. `package=com.zui` 不能命中 `com.zui.camera`（必须相等，不是子串）。
  3. 单字母别名 `t=` / `r=` / `d=` 要认 —— 早先被整条丢弃，
     于是 expect 变空、检查点永远"不校验"，判据静默失效。
  4. 路径图查 expect：查不到必须返回空串（交给第一轮探路），不能瞎猜。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.agent.nodes as ca  # noqa: E402


class _Ctx:
    skill_root = ROOT / "assets"


ca._ctx = lambda: _Ctx()   # 让路径图/checkpoint 能找到 assets

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


print("=== 1) activity 必须按段落边界匹配（手写 endswith 会错）===")
hit, why = ca.match_landing("activity=TimetableActivity",
                            activity=".timetable.management.EditTimetableActivity")
check("EditTimetableActivity 不命中 TimetableActivity", hit is False, why)

hit, why = ca.match_landing("activity=TimetableActivity",
                            activity=".timetable.display.TimetableActivity")
check("真正的 TimetableActivity 命中", hit is True, why)

hit, why = ca.match_landing("activity=EditTimetableActivity",
                            activity=".timetable.management.EditTimetableActivity")
check("EditTimetableActivity 命中自己", hit is True, why)

print()
print("=== 2) package 必须相等（不是子串）===")
hit, _ = ca.match_landing("package=com.zui.camera", state_pkg="com.zui.camera")
check("包名相等 -> 命中", hit is True)
hit, _ = ca.match_landing("package=com.zui", state_pkg="com.zui.camera")
check("com.zui 不命中 com.zui.camera", hit is False)
hit, _ = ca.match_landing("package=com.zui.camera", state_pkg="")
check("前台包名未知 -> 不命中", hit is False)

print()
print("=== 3) 单字母别名 t= r= d= 不能丢 ===")
nodes = [{"r": "btnCreateManually", "t": "手动创建课程表"}]
hit, why = ca.match_landing("t=手动创建课程表", clickable=nodes)
check("t= 别名可用", hit is True, why)
hit, why = ca.match_landing("r=btnCreateManually", clickable=nodes)
check("r= 别名可用", hit is True, why)
hit, why = ca.match_landing("rid=cv_empty_content", clickable=nodes)
check("不存在的 rid 不命中", hit is False, why)

print()
print("=== 4) AND / OR 语义 ===")
hit, why = ca.match_landing("activity=TimetableActivity,rid=btnCreateManually",
                            activity=".x.TimetableActivity", clickable=nodes)
check("逗号 = AND，全中才算命中", hit is True, why)
hit, why = ca.match_landing("activity=OtherActivity,rid=btnCreateManually",
                            activity=".x.TimetableActivity", clickable=nodes)
check("AND 缺一个就不命中", hit is False, why)
hit, why = ca.match_landing("activity=OtherActivity|TimetableActivity",
                            activity=".x.TimetableActivity")
check("竖线 = 同一判据的 OR", hit is True, why)

print()
print("=== 5) 路径图查 expect（不猜，查不到给空串）===")
exp = ca._pathmap_expect("com.zui.calendar", "btnCreateManually")
check("btnCreateManually 能查到落点", bool(exp), exp)
check("落点指向编辑页",
      "EditTimetableActivity" in exp, exp)

exp2 = ca._pathmap_expect("com.zui.calendar", "save_view")
check("save_view 能查到落点", bool(exp2), exp2)
check("落点回到周视图", "TimetableActivity" in exp2, exp2)

exp3 = ca._pathmap_expect("com.zui.calendar", "这个rid不存在")
check("查不到 -> 空串（交给第一轮探路）", exp3 == "", repr(exp3))

exp4 = ca._pathmap_expect("com.not.exist.pkg", "btnCreateManually")
check("没有路径图 -> 空串", exp4 == "", repr(exp4))

print()
print("=== 6) prepare 解析出来的动作要带上 expect ===")
do = ("① 点 `btnCreateManually` 进 `EditTimetableActivity`；"
      "② 点 `et_schedule_name` 聚焦，再用 "
      '`act input --x 1 --y 2 --text="<名称>"` 写入；'
      "③ 点 `save_view` 保存")
acts = ca._parse_prepare_actions(do, package="com.zui.calendar")
rids = [a["rid"] for a in acts]
check("动作顺序：建 -> 聚焦 -> 输入 -> 保存",
      rids == ["btnCreateManually", "et_schedule_name", "et_schedule_name", "save_view"],
      str(rids))
check("btnCreateManually 带 expect", bool(acts[0].get("expect")), acts[0].get("expect"))
check("save_view 带 expect", bool(acts[-1].get("expect")), acts[-1].get("expect"))
inp = next((a for a in acts if a["op"] == "input"), {})
check("输入值已转成 ASCII（adb 不支持中文）",
      inp.get("text") == "TestTimetable", repr(inp.get("text")))

print()
if FAIL:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
    raise SystemExit(1)
print(f"全部通过（{PASS} 项）")
