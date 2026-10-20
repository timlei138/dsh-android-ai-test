"""结构化判据（直接复用用例 YAML 的 check 形状）。

用例 YAML 的 check 是**结构化的**：
    check:
      activity: ".timetable.display.TimetableActivity"
      node_text: {rid: toolbar_title, equals: "课程表"}
      node_present: {rid: btnImportFromGallery}
      dialog_present: {rid: button1, text: "知道了"}
      package: "com.android.providers.media.module"

这些是纯字符串比对 —— **代码判得比模型准**（模型会看错、会拿"当前"当"到达过"）。
所以能判的一律代码判，判不了的（要看图的）才交回模型。

这个测试盯两件事：
    1) 该判对的判对（不能漏判、不能误判）
    2) **不认识的判据键必须显式报错** ——
       用例 YAML 自己写着「两者对不上时不会报错，只会静默跑偏」
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.agent.nodes import evaluate_check  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


def st(**over):
    s = {
        "last_activity": ".timetable.display.TimetableActivity",
        "last_package": "com.zui.calendar",
        "visited": [".AllInOneActivity -> .timetable.display.TimetableActivity"],
        "obs_clickable": [{"r": "btnImportFromGallery", "t": "从图库导入课程表"},
                          {"r": "toolbar_title", "t": "课程表"}],
        "obs_nodes_text": "课程表\n从图库导入课程表",
        "seen_rids": ["iv_more", "btnImportFromGallery"],
    }
    s.update(over)
    return s


print("=== 1) activity ===")
v, why = evaluate_check({"activity": ".timetable.display.TimetableActivity"}, state=st())
check("到达过 -> True", v is True, why)
v, why = evaluate_check({"activity": ".NoSuchActivity"}, state=st())
check("没到过 -> False", v is False, why)
v, why = evaluate_check({"activity_changed_to": "TimetableActivity"}, state=st())
check("activity_changed_to 同义", v is True, why)

print("\n=== 2) node_present / node_absent ===")
v, why = evaluate_check({"node_present": {"rid": "btnImportFromGallery"}}, state=st())
check("当前屏有 -> True", v is True, why)
v, why = evaluate_check({"node_present": {"rid": "iv_more"}}, state=st())
check("只在历史里见过 -> 也算 True（过程中出现过）", v is True, why)
v, why = evaluate_check({"node_present": {"rid": "never_existed"}}, state=st())
check("从没见过 -> False", v is False, why)
v, why = evaluate_check({"node_absent": {"rid": "never_existed"}}, state=st())
check("node_absent 成立 -> True", v is True, why)

print("\n=== 3) node_text ===")
v, why = evaluate_check(
    {"node_text": {"rid": "toolbar_title", "equals": "课程表"}}, state=st())
check("文案相符 -> True", v is True, why)
v, why = evaluate_check(
    {"node_text": {"rid": "toolbar_title", "equals": "设置"}}, state=st())
check("文案不符 -> False", v is False, why)

print("\n=== 4) package ===")
v, why = evaluate_check({"package": "com.zui.calendar"}, state=st())
check("包名相符 -> True", v is True, why)
v, why = evaluate_check({"package": "com.android.providers.media.module"}, state=st())
check("包名不符 -> False", v is False, why)

print("\n=== 5) 多判据 = AND ===")
v, why = evaluate_check(
    {"activity": ".timetable.display.TimetableActivity",
     "node_present": {"rid": "btnImportFromGallery"}}, state=st())
check("全成立 -> True", v is True, why)
v, why = evaluate_check(
    {"activity": ".timetable.display.TimetableActivity",
     "node_present": {"rid": "never_existed"}}, state=st())
check("一条不成立 -> False", v is False, why)

print("\n=== 6) 注解字段不参与判定（不能因为 note 就改判）===")
v, why = evaluate_check(
    {"activity": ".timetable.display.TimetableActivity",
     "note": "这是给人看的说明", "measured": "17 个"}, state=st())
check("注解被忽略，结论仍 True", v is True, why)

print("\n=== 7) ★ 不认识的判据键必须报出来（不静默跑偏）===")
v, why = evaluate_check({"activity": "TimetableActivity",
                         "wheel_upper_bound": 12}, state=st())
check("结果仍按认得的算", v is True, why)
check("但明确提示了不认识的键",
      "不认识的判据键" in why and "wheel_upper_bound" in why, why)

v, why = evaluate_check({"slot_count_cap": 8}, state=st())
check("全是陌生键 -> 返回 None（交回模型）且提示", v is None and "不认识的判据键" in why, why)

print("\n=== 8) 判不了的（要看图）交回模型 ===")
v, why = evaluate_check(None, state=st())
check("无判据 -> None", v is None, why)
v, why = evaluate_check("", state=st())
check("空判据 -> None", v is None, why)

print("\n=== 9) 字符串形式仍兼容（老用例不受影响）===")
v, why = evaluate_check("activity=TimetableActivity", state=st())
check("activity= 字符串 -> True", v is True, why)
v, why = evaluate_check("rid=btnImportFromGallery", state=st())
check("rid= 字符串 -> True", v is True, why)

print("\n=== 10) 弹框判据 ===")
v, why = evaluate_check({"dialog_present": {"rid": "btnImportFromGallery"}}, state=st())
check("弹框目标在屏上 -> True", v is True, why)
v, why = evaluate_check({"dialog_present": {"rid": "no_dialog"}}, state=st())
check("弹框不在 -> False", v is False, why)

print(f"\n{'=' * 50}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 50}")
sys.exit(1 if fail else 0)
