"""回归：activity= 判据必须按"到达过"判，不是"当前还在"。

真机踩到（168 实跑）：a1 判据是
    activity=.timetable.display.TimetableActivity
但断言是**流程走完后**统一判的，那时屏幕上已是系统图库选择器。
模型照"当前 activity"判了 FAIL —— 可日志显示第 5-8 轮确实停在过课程表页。

结论：判定没错，是**判据被误用**。修法是把"到达过没有"变成代码算的硬事实。

契约：
  1. 路径里出现过 -> True（模型判 FAIL 时被纠正为 PASS）
  2. 路径里从没出现 -> False（模型判 PASS 时被纠正为 FAIL）
  3. 非 activity= 判据 -> None（不干预）
  4. node_present= 仍走 seen_rids 那条路，不受影响
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import android_agent.agent.nodes as ca

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


# 复刻真机的 visited 形状
STATE = {
    "visited": [
        "com.zui.calendar/.AllInOneActivity -> com.zui.calendar/.timetable.display.TimetableActivity",
        "com.zui.calendar/.timetable.display.TimetableActivity -> com.android.providers.media.photopicker.PhotoPickerGetContentActivity",
    ],
    "last_activity": "com.android.providers.media.photopicker.PhotoPickerGetContentActivity",
}

print("=== 1. 到过（哪怕现在不在了）-> True ===")
r = ca._activity_reached(STATE, "activity=.timetable.display.TimetableActivity")
check("TimetableActivity 判为到达过", r is True, str(r))

print("\n=== 2. 当前所在的 activity 也认 ===")
r = ca._activity_reached(STATE, "activity=PhotoPickerGetContentActivity")
check("PhotoPicker 判为到达过", r is True, str(r))

print("\n=== 3. 从没出现过的 -> False ===")
r = ca._activity_reached(STATE, "activity=.SomeNeverVisitedActivity")
check("没到过判 False", r is False, str(r))

print("\n=== 4. 非 activity 判据 -> None（不干预）===")
r = ca._activity_reached(STATE, "node_present=rid=btnImportFromGallery")
check("node_present 返回 None", r is None, str(r))
r = ca._activity_reached(STATE, "界面显示课程表")
check("文字判据返回 None", r is None, str(r))
r = ca._activity_reached(STATE, "")
check("空判据返回 None", r is None, str(r))

print("\n=== 5. 子串匹配（判据常写短名）===")
r = ca._activity_reached(STATE, "activity=TimetableActivity")
check("短名也能匹配", r is True, str(r))

print("\n=== 6. 空 visited 不炸 ===")
r = ca._activity_reached({"visited": [], "last_activity": None},
                         "activity=Foo")
check("空状态判 False（不抛异常）", r is False, str(r))

print("\n" + "=" * 46)
print(f"  全部通过（{len(PASS)} 项）" if not FAIL
      else f"  {len(FAIL)} 项失败: {FAIL}")
print("=" * 46)
raise SystemExit(0 if not FAIL else 1)
