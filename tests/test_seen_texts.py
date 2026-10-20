"""回归：断言在流程**末尾**判时，历史文案不能丢。

真机踩到的 bug（168 的 a1）：
    a1 要求  node_text: {rid: toolbar_title, equals: "课程表"}
    第 5 轮确实到了课程表页，toolbar_title={"t": "课程表"} 就在屏上。
    但断言是**流程走完后**统一判的，那时人已经翻到系统图库，
    当前屏里当然没有 toolbar_title —— 于是 a1 被判"不成立"、从 PASS 改判 FAIL。

**"代码判得比模型准"的反面**：读错范围的代码，比模型还瞎。
模型判 PASS 反而是对的，是代码把它纠正错了。

根因：只攒了 rid（seen_rids），没攒文案。
修法：加 seen_texts，判 node_text 时先看历史。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.agent.nodes import _seen_texts, evaluate_check  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


CHECK = {"activity": ".timetable.display.TimetableActivity",
         "node_text": {"rid": "toolbar_title", "equals": "课程表"}}

# 判的时候：人在系统图库，当前屏没有 toolbar_title
END_STATE = {
    "last_activity": "com.android.providers.media.photopicker."
                     "PhotoPickerGetContentActivity",
    "last_package": "com.android.providers.media.module",
    "visited": [
        "com.zui.calendar/.AllInOneActivity -> "
        "com.zui.calendar/.timetable.display.TimetableActivity",
    ],
    "obs_clickable": [],            # ← 当前屏（图库）里没有 toolbar_title
    "obs_nodes_text": "取消\n相册\n更多选项",
    "seen_rids": ["iv_more", "toolbar_title", "btnImportFromGallery"],
    "seen_texts": [                 # ← 过程中见过的文案
        {"rid": "iv_more", "text": ""},
        {"rid": "toolbar_title", "text": "课程表"},
        {"rid": "btnImportFromGallery", "text": "从图库导入课程表"},
    ],
}

print("=== 1) _seen_texts 提取得对吗 ===")
got = _seen_texts([{"r": "toolbar_title", "t": "课程表"},
                   {"r": "iv_more"},
                   {"t": "没有 rid 的项"}])
check("提取带 rid 的项", len(got) == 2, str(got))
check("文案读到了", got[0] == {"rid": "toolbar_title", "text": "课程表"},
      str(got[0]))
check("没有文案的项 text 为空串（不是 None）",
      got[1] == {"rid": "iv_more", "text": ""}, str(got[1]))
check("没 rid 的项被跳过（判据按 rid 找）", len(got) == 2, str(got))
check("空输入 -> 空列表", _seen_texts(None) == [], str(_seen_texts(None)))

print("\n=== 2) ★ 核心回归：末尾判时，历史文案要能救回 a1 ===")
v, why = evaluate_check(CHECK, state=END_STATE)
check("a1 判为成立（不再误判 FAIL）", v is True, why)
check("说明里点出用的是历史文案",
      "toolbar_title" in why and "课程表" in why, why)

print("\n=== 3) 但不该无脑放行：文案真的不对时必须判不成立 ===")
bad = {**END_STATE,
       "seen_texts": [{"rid": "toolbar_title", "text": "设置"}]}
v, why = evaluate_check(CHECK, state=bad)
check("历史上写的是别的字 -> 不成立", v is False, why)
check("说明里给出实际文案（便于定位）", "设置" in why, why)

print("\n=== 4) 从没见过的控件 -> 不成立（不能因为'没见过'就放过）===")
never = {**END_STATE, "seen_texts": [{"rid": "iv_more", "text": ""}],
         "seen_rids": ["iv_more"]}
v, why = evaluate_check(CHECK, state=never)
check("没见过 toolbar_title -> 不成立", v is False, why)

print("\n=== 5) 历史优先于当前屏（当前屏看不到时靠历史）===")
both = {**END_STATE,
        "obs_clickable": [{"r": "toolbar_title", "t": "别的字"}],
        "seen_texts": [{"rid": "toolbar_title", "text": "课程表"}]}
v, why = evaluate_check(CHECK, state=both)
check("历史里有「课程表」-> 成立", v is True, why)

print("\n=== 6) 老行为不变：没有 seen_texts 时仍看当前屏 ===")
old = {"last_activity": "com.zui.calendar/.timetable.display.TimetableActivity",
       "visited": [], "obs_clickable": [{"r": "toolbar_title", "t": "课程表"}],
       "obs_nodes_text": "课程表"}
v, why = evaluate_check(CHECK, state=old)
check("当前屏有 -> 成立（向后兼容）", v is True, why)

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
