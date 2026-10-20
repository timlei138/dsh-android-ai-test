"""测 decision_channel：确认它真能拿到合法决策，且守住坐标纪律。

三件事要验：
  1. 正常模式能选出坐标，且坐标来自给定列表
  2. 弹窗场景能自动点掉（不因没见过的弹窗停下）
  3. 硬塞一个不存在的坐标，必须被 _validate_action 拦下
"""
import sys
from pathlib import Path

# Windows 控制台默认 GBK，直接打 ✓/✗ 会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import android_agent.agent.decide as dc

from android_agent.cases.paths import runner_python, skill_root  # noqa: E402
SKILL = skill_root()
PY = runner_python()

print("=== 0. 配置能读到吗 ===")
try:
    cfg = dc._load_config(SKILL)
    print(f"  base_url={cfg['base_url']}  model={cfg['model']}  "
          f"key={cfg['api_key'][:8]}...")
except Exception as e:
    print(f"  ✗ {e}")
    sys.exit(1)

print("\n=== 1. 正常模式：下一步点哪 ===")
payload = {
    "case_id": "168",
    "case_title": "日历课程表",
    "step_index": 0,
    "step_total": 3,
    "step_desc": "打开日历，进入课程表",
    "step_locate": "首页的更多选项菜单",
    "step_landing": "activity=TimetableGuideActivity",
    "step_expect": "看到课程表引导页",
    "activity": "AllInOneActivity",
    "round": 1,
    "max_rounds": 40,
    "history": [],
    "clickable": [
        {"r": "com.zui.calendar:id/btnMore", "c": [2170, 1850]},
        {"r": "com.zui.calendar:id/btnSearch", "c": [1850, 1850]},
        {"r": "com.zui.calendar:id/title", "t": "日历", "c": [540, 120]},
        {"t": "今天", "c": [300, 900]},
    ],
}
try:
    a = dc.decide_step(PY, SKILL, payload)
    print(f"  step_done={a['step_done']}  next=({a['next_x']},{a['next_y']})")
    print(f"  why={a['why']}")
    print(f"  expect={a['expect']}")
    allowed = {dc._parse_coord(c) for c in payload["clickable"]}
    if a["step_done"] or (a["next_x"], a["next_y"]) in allowed:
        print("  ✓ 坐标来自给定列表（或判定完成）")
    else:
        print("  ✗ 坐标越界！")
except Exception as e:
    print(f"  ✗ {type(e).__name__}: {e}")

print("\n=== 2. 弹窗场景：应自动点掉而非放弃 ===")
payload2 = {
    "case_id": "168",
    "case_title": "日历课程表",
    "step_index": 0,
    "step_total": 3,
    "step_desc": "打开日历",
    "step_landing": "activity=AllInOneActivity",
    "step_expect": "进入日历首页",
    "activity": "PermissionActivity",
    "round": 1,
    "max_rounds": 40,
    "history": [],
    "clickable": [
        {"r": "android:id/button1", "c": [2170, 1314]},
        {"r": "android:id/button2", "c": [1070, 1314]},
        {"r": "android:id/alertTitle", "t": "权限说明", "c": [1620, 900]},
    ],
}
try:
    a = dc.decide_step(PY, SKILL, payload2)
    print(f"  next=({a['next_x']},{a['next_y']})  why={a['why']}")
    if (a["next_x"], a["next_y"]) == (2170, 1314):
        print("  ✓ 选了 button1（同意/确定）—— 自动点掉弹窗")
    else:
        print(f"  ⚠ 选了别的：{a['why']}")
except Exception as e:
    print(f"  ✗ {type(e).__name__}: {e}")

print("\n=== 3. 坐标纪律：给个不存在的坐标必须被拦 ===")
try:
    dc._validate_action(
        {"step_done": False, "next_x": 99999, "next_y": 88888, "why": "瞎编的"},
        ["a@[100,200]", "b@[300,400]"])
    print("  ✗ 没拦住！这是严重问题")
except dc.DecisionError as e:
    print(f"  ✓ 拦住了：{str(e)[:90]}")
