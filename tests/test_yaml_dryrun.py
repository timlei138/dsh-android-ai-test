"""端到端干跑：YAML -> spec -> 图 -> 但**不碰真机**。

目标：在花 3-5 分钟真机跑之前，先确认
    1) YAML 能读进来
    2) 判据能按代码判死（不是丢给模型）
    3) 有界重试接在正确的位置

为什么必须先干跑：上次插件漏写 render，白跑了整轮设备才发现。
凡是"不用设备就能发现的问题"，都不该用设备去发现。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.cases.loader import load_case_by_name  # noqa: E402
from android_agent.agent.nodes import evaluate_check, is_deterministic_landing  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


print("=== 1) 读 168 ===")
spec, warns, path = load_case_by_name("168")
check("读到了", spec is not None, path.name)
check("3 步 3 断言", len(spec.steps) == 3 and len(spec.asserts) == 3)

print("\n=== 2) 168 的三条断言，代码能判吗 ===")
# 造一个"跑成功了"的状态
good = {
    "last_activity": "com.android.providers.media.photopicker."
                      "PhotoPickerGetContentActivity",
    "last_package": "com.android.providers.media.module",
    "visited": [
        "com.zui.calendar/.AllInOneActivity -> "
        "com.zui.calendar/.timetable.display.TimetableActivity",
        "com.zui.calendar/.timetable.display.TimetableActivity -> "
        "com.android.providers.media.photopicker."
        "PhotoPickerGetContentActivity",
    ],
    "obs_clickable": [{"r": "btnImportFromGallery", "t": "从图库导入课程表"},
                      {"r": "toolbar_title", "t": "课程表"}],
    "obs_nodes_text": "课程表\n从图库导入课程表",
    "seen_rids": ["iv_more", "btnImportFromGallery", "button1"],
}

for a in spec.asserts:
    v, why = evaluate_check(a.check, state=good)
    check(f"{a.id} 能由代码判定", v is not None,
          f"{'成立' if v else '不成立'}：{why}")

print("\n=== 3) 跑失败的状态，代码也要判得出来（不能只会说通过）===")
bad = {**good,
       "last_activity": "com.zui.calendar/.AllInOneActivity",
       "last_package": "com.zui.calendar",
       "visited": ["com.zui.calendar/.AllInOneActivity"],
       "obs_clickable": [{"r": "iv_more"}],
       "seen_rids": ["iv_more"]}
for a in spec.asserts:
    v, why = evaluate_check(a.check, state=bad)
    check(f"{a.id} 判为不成立", v is False, why)

print("\n=== 4) a2 的 after_tap 子判据（嵌套）===")
a2 = [a for a in spec.asserts if a.id == "a2"][0]
v, why = evaluate_check(a2.check, state=good)
check("a2 有结论", v is not None, why)
if "after_tap" in (a2.check or {}):
    check("a2 的 after_tap 已实现（递归展开判）",
          "点击后" in why, why)
    # after_tap 里的弹框 rid 不在 good 状态里时，必须判不成立
    bad2 = {**good, "seen_rids": ["iv_more", "btnImportFromGallery"],
            "obs_clickable": [{"r": "btnImportFromGallery"}]}
    v2, why2 = evaluate_check(a2.check, state=bad2)
    check("弹框没出现过 -> a2 判不成立", v2 is False, why2)

print("\n=== 5) 每一步的强证据目标与落点都解析出来了 ===")
for s in spec.steps:
    check(f"{s.id} 有落点", bool(s.landing), str(s.landing))
    # ★ 换了关注点：早先验的是 locate_seq 里存了几个动作（按序重放的旧机制）。
    #   现在没有回放了，靠的是**每步一个可唯一判定的 target**：
    #   命中恰好 1 个 -> 代码直点；0 个或 >1 个 -> 交模型。
    #   所以真正要验的是"target 写成了代码能唯一判定的形态"。
    check(f"{s.id} 有 target", bool(s.target), str(s.target))
    det, why = is_deterministic_landing(s.target or "")
    check(f"{s.id} target 可唯一判定", det, why)

print("\n=== 6) 前提会执行 pm clear ===")
check("有 pm clear", any("pm clear" in c for pc in spec.preconditions
                         for c in pc.do))

print("\n=== 7) 序列化成 JSON 能过（插件就是这么传的）===")
try:
    blob = json.dumps(spec.model_dump(), ensure_ascii=False)
    check("可序列化", len(blob) > 100, f"{len(blob)} 字节")
    back = json.loads(blob)
    check("断言判据往返后仍是字典（没被字符串化）",
          isinstance(back["asserts"][0]["check"], dict),
          str(type(back["asserts"][0]["check"]).__name__))
except Exception as e:
    check("可序列化", False, f"{type(e).__name__}: {e}")

print(f"\n{'=' * 52}")
print(f"  {'干跑通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
