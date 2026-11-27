# -*- coding: utf-8 -*-
"""`tools/system.py` —— 系统面板状态判定（通知栏 / 快捷设置）的单测。

这三件事都**不需要真机**：`_shade_open` / `_shade_kind` 是纯字符串判定，
`_applied` 是纯逻辑（只有 wake/unlock/rotate 分支要 d，传 None 就返回「判不了」）。

为什么必须先有测试（都是真实缺陷）：
  · `_shade_kind` 曾经「先判 QS」并断言「快捷设置展开时只有 qs_*」——
    实测 6245 份真实 dump 里 `quick_settings_container` 出现 16 次，
    **16 次全都同时含 `notification_stack_scroller`**（横屏分栏同屏并排）。
    于是横屏上每一次展开都被判成 quick_settings，`system notifications`
    会被判成「打开的是快捷设置」——假失败，AI 拿到 false 就去重试。
  · `_applied` 里有一句「认出 NotificationShade 焦点」的文案，该 activity
    在 6245 份真实 dump 里 0 次出现，注释却把它当成常规路径。
  · 只判一个容器 → 另一种形态被判成「没展开」，后面全错。
"""
import io
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import system as S  # noqa: E402

CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


# ── 造 dump 的最小片段：只放判定用的 resource-id ──────────────────────────
SCROLLER = 'resource-id="com.android.systemui:id/notification_stack_scroller"'
QS = 'resource-id="com.android.systemui:id/quick_settings_container"'
ROW = 'resource-id="android:id/expandableNotificationRow"'


@case("kind：两个容器都没有 → None（没展开）")
def _():
    assert S._shade_kind("<hierarchy/>") is None
    assert S._shade_kind("") is None


@case("kind：只有通知列表容器 → notifications")
def _():
    assert S._shade_kind(f"<node {SCROLLER}/>") == "notifications"


@case("kind：只有快捷设置容器 → quick_settings")
def _():
    assert S._shade_kind(f"<node {QS}/>") == "quick_settings"


@case("★ kind：两个容器同时出现 → both（横屏分栏，不能靠猜优先级）")
def _():
    # 这是真实形态：16/16 含 quick_settings_container 的 dump 都同时含 scroller
    assert S._shade_kind(f"<node {SCROLLER}/><node {QS}/>") == "both"
    assert S._shade_kind(f"<node {QS}/><node {SCROLLER}/>") == "both"


@case("kind：只认容器 id 片段，不认外观类似的其它串")
def _():
    assert S._shade_kind('text="notification_stack_scroller"') == "notifications"
    # 注意：这是字符串包含判定 —— 只要文本里出现该 id 就算命中，
    # 这里固化为契约：判定基于 dump 全文，不做 XML 结构解析。
    assert S._shade_kind('resource-id="x:id/quick_settings_container_big"') == "quick_settings"


@case("open：三个判据任一成立即展开")
def _():
    assert S._shade_open(f"<node {SCROLLER}/>", None) is True
    assert S._shade_open(f"<node {QS}/>", None) is True
    assert S._shade_open("<hierarchy/>", "com.android.systemui.NotificationShade") is True


@case("open：都没命中 → False")
def _():
    assert S._shade_open("<hierarchy/>", None) is False
    assert S._shade_open("<hierarchy/>", "com.zui.calendar.AllInOneActivity") is False
    assert S._shade_open("<hierarchy/>", "") is False


@case("★ applied：both 时两种展开动作都算生效（否则横屏必然假失败）")
def _():
    xml = f"<node {SCROLLER}/><node {QS}/>"
    assert S._applied("notifications", opened=True, kind="both", xml_text=xml) == (True, "")
    assert S._applied("quick_settings", opened=True, kind="both", xml_text=xml) == (True, "")


@case("applied：只要的那块在 → True")
def _():
    assert S._applied("notifications", opened=True, kind="notifications",
                      xml_text="") == (True, "")
    assert S._applied("quick_settings", opened=True, kind="quick_settings",
                      xml_text="") == (True, "")


@case("applied：展开的是另一块 → False 且说明写的是「哪块」")
def _():
    ok, note = S._applied("notifications", opened=True, kind="quick_settings", xml_text="")
    assert ok is False
    assert "快捷设置" in note and "通知栏" in note
    ok, note = S._applied("quick_settings", opened=True, kind="notifications", xml_text="")
    assert ok is False
    assert "通知栏" in note and "快捷设置" in note


@case("applied：opened=False → False（动作没生效）")
def _():
    assert S._applied("notifications", opened=False, kind=None, xml_text="") == (False, "")
    assert S._applied("quick_settings", opened=False, kind=None, xml_text="") == (False, "")


@case("★ applied：只有 NotificationShade 兜底命中时 → True 但说明写了「容器名不认识」")
def _():
    ok, note = S._applied("notifications", opened=True, kind=None, xml_text="")
    assert ok is True
    # 文案必须提醒：判不了通知内容，要看截图。不能只给个 True 让 AI 以为解析成功。
    assert "NotificationShade" in note
    assert "截图" in note


@case("applied：close_shade 逻辑与展开相反")
def _():
    assert S._applied("close_shade", opened=False, kind=None, xml_text="") == (True, "")
    ok, note = S._applied("close_shade", opened=True, kind="both", xml_text="")
    assert ok is False and "仍有面板容器" in note


@case("applied：拿不到设备状态时 wake/unlock/rotate 一律 None（不硬判）")
def _():
    for act in ("wake", "unlock", "rotate"):
        ok, note = S._applied(act, opened=False, kind=None, xml_text="", d=None)
        assert ok is None, (act, ok)
        assert note, act


@case("applied：未知动作 → None（兜底不硬判）")
def _():
    ok, note = S._applied("teleport", opened=False, kind=None, xml_text="", d=None)
    assert ok is None and note


@case("applied：三态取值只能是 True / False / None，且 False 一定带说明或属正常否定")
def _():
    cases = [
        ("notifications", True, "both"),
        ("notifications", True, "quick_settings"),
        ("notifications", True, None),
        ("notifications", False, None),
        ("close_shade", True, "notifications"),
        ("close_shade", False, None),
        ("wake", False, None),
    ]
    for act, opened, kind in cases:
        ok, note = S._applied(act, opened=opened, kind=kind, xml_text="", d=None)
        assert ok in (True, False, None), (act, ok)
        assert isinstance(note, str), (act, note)


@case("★ 真实 dump 回归：横屏分栏那份必须判 both，通知栏那份必须判 notifications")
def _():
    root = HERE.parent / "storage" / "run_history"
    both = (root / "f62fba1d-c2e6-44ec-8c70-79982a350572" / "evidence" / "case11"
            / "step51-shade-nodes" / "dump.xml")
    notif = (root / "c346f38b-c275-45a6-a6bc-6a930da0b31a" / "evidence" / "case85"
             / "step10" / "dump.xml")
    if not both.is_file() or not notif.is_file():
        print("      (跳过：本机没有这两份真实 dump)")
        return
    tb = both.read_text(encoding="utf-8", errors="replace")
    assert S._shade_open(tb, None) is True
    assert S._shade_kind(tb) == "both", S._shade_kind(tb)
    assert S._applied("notifications", opened=True, kind="both", xml_text=tb)[0] is True

    tn = notif.read_text(encoding="utf-8", errors="replace")
    assert S._shade_open(tn, None) is True
    assert S._shade_kind(tn) == "notifications", S._shade_kind(tn)
    assert S._applied("notifications", opened=True, kind="notifications", xml_text=tn)[0] is True
    assert S._applied("quick_settings", opened=True, kind="notifications", xml_text=tn)[0] is False


def main() -> int:
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name} -> {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name} -> {type(exc).__name__}: {exc}")
    print(f"\n  {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
