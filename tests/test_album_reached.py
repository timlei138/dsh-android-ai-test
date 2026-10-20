# -*- coding: utf-8 -*-
"""判"打开了相册"，而不是"打开了那个类名的相册"。

## 这个测试防的是什么

168 的 a3 原本写死
    activity: com.android.providers.media.photopicker.PhotoPickerGetContentActivity
真机 Android 17 给的是 ...PhotoPickerUserSelectActivity，直接判 FAIL ——
**功能是对的，判据是错的**。

用户的原话：
    「打开相册。我不管打开的是哪个相册。打开就行了。」

所以判据要按"这一屏是不是选图/相册类界面"来判，
而不是"是不是那个具体实现类"。这里把各种真实会遇到的界面都过一遍，
包括**必须判假的**那些 —— 判宽了比判严了更危险，
权限框、桌面被当成"已打开相册"就是漏测。
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


from android_agent.agent.nodes import (  # noqa: E402
    _album_reached, evaluate_check, match_landing)


def _st(*pages: str) -> dict:
    """造一个"走过这些页面"的 state。visited 用真实格式 a -> b。"""
    visited = []
    for i in range(len(pages) - 1):
        visited.append(f"{pages[i]} -> {pages[i+1]}")
    return {"visited": visited, "last_activity": pages[-1] if pages else "",
            "seen_rids": [], "seen_screens": [], "obs_clickable": [],
            "obs_nodes_text": "", "last_package": ""}


print("=" * 70)
print("1) 真的打开相册 —— 各种实现都得认")
print("=" * 70)

# 每一条都是真实存在过的选图界面命名方式
_ALBUMS = [
    ("Android 17 实际给的（旧用例判死在这里）",
     "com.android.providers.media.photopicker.PhotoPickerUserSelectActivity"),
    ("用例原本写死的那个",
     "com.android.providers.media.photopicker.PhotoPickerGetContentActivity"),
    ("AOSP 早期命名",
     "com.android.providers.media.photopicker.PhotoPickerActivity"),
    ("小米/部分 ROM 的媒体选择器",
     "com.miui.gallery.activity.ExternalPhotoPickerActivity"),
    ("DocumentsUI 当选图器",
     "com.android.documentsui.picker.PickActivity"),
    ("相册 App",
     "com.google.android.apps.photos.album.AlbumActivity"),
    ("厂商相册",
     "com.zui.gallery.app.GalleryActivity"),
]

for label, act in _ALBUMS:
    ok, where = _album_reached(_st("com.zui.calendar.AllInOneActivity", act))
    ck(f"{label}：{'…' + act.split('.')[-1]}", ok, where)

print()
print("=" * 70)
print("2) 不是相册 —— 必须判假（判宽了就是漏测）")
print("=" * 70)

_NOT_ALBUMS = [
    ("系统权限框", "com.android.permissioncontroller.GrantPermissionsActivity"),
    ("日历首页", "com.zui.calendar.AllInOneActivity"),
    ("课程表", "com.zui.calendar.timetable.display.TimetableActivity"),
    ("桌面", "com.zui.launcher.Launcher"),
    ("编辑页", "com.zui.calendar.timetable.edit.EditTimetableActivity"),
]

for label, act in _NOT_ALBUMS:
    ok, why = _album_reached(_st(act))
    ck(f"{label} 不算打开相册", not ok, why)

print()
print("=" * 70)
print("3) 判「到达过」而不是「现在在哪」")
print("=" * 70)

# 到了相册，但收尾时又退回报错页 —— 仍然算到达过
st = _st("com.zui.calendar.AllInOneActivity",
         "com.android.providers.media.photopicker.PhotoPickerUserSelectActivity",
         "com.zui.calendar.AllInOneActivity")
ok, why = _album_reached(st)
ck("★ 到了相册又退回来，仍算达成", ok, why)

# 从没到过
st2 = _st("com.zui.calendar.AllInOneActivity",
          "com.zui.calendar.timetable.display.TimetableActivity")
ok2, why2 = _album_reached(st2)
ck("★ 全程没到过相册，判不成立", not ok2, why2)
ck("失败说明里说清停在哪", "最后停在" in why2, why2)

print()
print("=" * 70)
print("4) 权限框不能顶替相册（曾经的真实风险）")
print("=" * 70)
# 包名 com.android.providers.media.module 是图库进程，但这一屏是权限弹窗
st3 = _st("com.zui.calendar.AllInOneActivity",
          "com.android.permissioncontroller.GrantPermissionsActivity")
ok3, why3 = _album_reached(st3)
ck("权限框不算", not ok3, why3)

print()
print("=" * 70)
print("5) 走 evaluate_check（用例 YAML 里真正走的那条路）")
print("=" * 70)
st = _st("com.zui.calendar.AllInOneActivity",
         "com.android.providers.media.photopicker.PhotoPickerUserSelectActivity")
v, why = evaluate_check({"album_reached": True}, state=st)
ck("album_reached: true 判 PASS", v is True, why)
ck("说明里带出实际打开的是哪个", "PhotoPickerUserSelectActivity" in why, why)

v2, why2 = evaluate_check({"album_reached": True}, state=_st("com.zui.calendar.AllInOneActivity"))
ck("没打开相册判 FAIL", v2 is False, why2)

# kinds 限定：只认系统选择器时，相册 App 不算
st_g = _st("com.zui.calendar.AllInOneActivity",
           "com.zui.gallery.app.GalleryActivity")
v3, why3 = evaluate_check({"album_reached": {"kinds": ["photopicker"]}}, state=st_g)
ck("kinds 能收窄范围（限定 photopicker 时相册 App 不算）", v3 is False, why3)
v4, why4 = evaluate_check({"album_reached": True}, state=st_g)
ck("不限定 kinds 时相册 App 也算", v4 is True, why4)

print()
print("=" * 70)
print("6) 落点判据 album=any")
print("=" * 70)
hit, why = match_landing("album=any",
                         activity="com.android.providers.media.photopicker.PhotoPickerUserSelectActivity")
ck("★ 落点 album=any 命中 Android 17 的选择器", hit, why)
hit2, why2 = match_landing("album=any", activity="com.zui.calendar.AllInOneActivity")
ck("日历首页不算落点", not hit2, why2)
hit3, why3 = match_landing("album=any",
                           activity="com.android.permissioncontroller.GrantPermissionsActivity")
ck("权限框不算落点", not hit3, why3)
hit4, why4 = match_landing("album=whatever", activity="com.zui.gallery.app.GalleryActivity")
ck("写错的值要报错，不能静默放行", not hit4 and "只支持 any" in why4, why4)

print()
print("=" * 70)
print("7) 08 份旧写法仍然有效（不能破坏既有用例）")
print("=" * 70)
hit5, why5 = match_landing(
    "activity=.timetable.display.TimetableActivity",
    activity="com.zui.calendar.timetable.display.TimetableActivity")
ck("activity= 判据没受影响", hit5, why5)
hit6, why6 = match_landing("activity=TimetableActivity",
                           activity="com.zui.calendar.timetable.edit.EditTimetableActivity")
ck("activity= 不做子串误配（Edit 不能被当成 Timetable）", not hit6, why6)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
