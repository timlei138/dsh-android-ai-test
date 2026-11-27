# -*- coding: utf-8 -*-
"""`common.knowledge_hint` / `common.knowledge_hint_key` 的去重粒度单测。

## 为什么值得测

知识卡提示是**唯一一处"给 AI 喂已沉淀事实"的通道**。它的去重单位一旦写错，
后果是静默的：AI 不会报错，只是从此再也看不到那张卡。

真实事故（N-05）：去重单位原来是 `sessions.knowledge_shown` 一个布尔 ——
整个会话只提示一次。真库 418/427 个会话都是「提示过一次就永久静默」。
于是「start 时提示了日历卡 → 走到系统设置页（settings 有专属卡）→ 不再提示」，
AI 只能凭经验猜系统界面行为，而 `_system.md` 里写的口径一直是"按前台包读卡"。

反方向的错更危险：如果按 activity 拼键，弹窗/编辑页会让同一个包的索引
反复出现，把压 nodes 省下的 token 全还回去（这也是当初只提示一次的原因）。

所以这里钉住三件事：
  ① 同一个包 → 只提示一次；
  ② **不同包各提示一次**（N-05 修复点，旧实现这里返回 None）；
  ③ 无专属卡的包共用 `system` 键，整个会话只提示一次。
"""
import io
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

import common  # noqa: E402

CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


# 真实存在的卡 / 真实不存在的包（见 assets/knowledge/）
CARDED = ["com.zui.calendar", "com.zui.launcher", "com.android.settings"]
NO_CARD = "com.no.such.package.xyz"


@case("前置：夹具里的卡真的存在，无卡包真的没卡")
def _():
    assert (ROOT / "assets" / "knowledge" / f"{CARDED[0]}.md").is_file()
    assert not (ROOT / "assets" / "knowledge" / f"{NO_CARD}.md").exists()


@case("无包名 → 键为空串，且不提示（start 时还不知道包名）")
def _():
    assert common.knowledge_hint_key(None) == ""
    assert common.knowledge_hint_key("") == ""
    assert common.knowledge_hint(None) is None
    assert common.knowledge_hint("") is None


@case("有专属卡 → 键是 card:<包名>；无专属卡 → 键统一是 system")
def _():
    for pkg in CARDED:
        assert common.knowledge_hint_key(pkg) == f"card:{pkg}"
    assert common.knowledge_hint_key(NO_CARD) == "system"


@case("★ 不同包算出不同键（N-05 的根：旧实现对第二个包直接返回 None）")
def _():
    keys = {common.knowledge_hint_key(p) for p in CARDED}
    assert len(keys) == len(CARDED), keys
    assert common.knowledge_hint_key(CARDED[0]) != common.knowledge_hint_key(CARDED[1])


@case("★ 无专属卡的包共用一个键（否则每个陌生包都重发一遍 system 提示）")
def _():
    assert common.knowledge_hint_key("com.a.b.c") == "system"
    assert common.knowledge_hint_key("com.x.y.z") == "system"


@case("同一 activity 不影响键：弹窗/编辑页不重发索引（按 activity 拼键会撑爆 token）")
def _():
    a = common.knowledge_hint_key("com.zui.calendar", ".TimetableActivity")
    b = common.knowledge_hint_key("com.zui.calendar", ".EditTimetableActivity")
    c = common.knowledge_hint_key("com.zui.calendar", None)
    assert a == b == c == "card:com.zui.calendar"


@case("未提示过 → 给出卡内容（package / sections / how_to_read 齐全）")
def _():
    h = common.knowledge_hint("com.zui.calendar", shown_keys=set())
    assert h is not None
    assert h.get("package") == "com.zui.calendar"
    assert isinstance(h.get("sections"), list) and h["sections"], h.get("sections")
    assert 'card="com.zui.calendar"' in (h.get("how_to_read") or "")


@case("已提示过该卡（键在 shown_keys 里）→ 返回 None")
def _():
    assert common.knowledge_hint("com.zui.calendar",
                                 shown_keys={"card:com.zui.calendar"}) is None


@case("★ 提示过日历 ≠ 提示过设置：第二个包仍然要给（这就是修复后的行为）")
def _():
    shown = {"card:com.zui.calendar"}
    h = common.knowledge_hint("com.android.settings", shown_keys=shown)
    assert h is not None, "不同包被判成已提示 = N-05 复发"
    assert h.get("package") == "com.android.settings"


@case("shown_keys=None（没传）→ 当作没提示过，照常给")
def _():
    assert common.knowledge_hint("com.zui.calendar", shown_keys=None) is not None


@case("无专属卡 → 给 system_card 提示（card=None），不是 None")
def _():
    h = common.knowledge_hint(NO_CARD, shown_keys=set())
    assert h is not None
    assert h.get("card") is None
    assert h.get("system_card")


@case("★ 已提示过 system → 另一个陌生包不再重复（否则每换个陌生 App 都刷一遍）")
def _():
    assert common.knowledge_hint("com.a.b.c", shown_keys={"system"}) is None
    assert common.knowledge_hint("com.x.y.z", shown_keys={"system"}) is None


@case("提示过某张卡不影响 system 键，反之亦然")
def _():
    assert common.knowledge_hint("com.a.b.c",
                                 shown_keys={"card:com.zui.calendar"}) is not None
    assert common.knowledge_hint("com.zui.calendar",
                                 shown_keys={"system"}) is not None


@case("键是纯字符串、可直接排序落库（JSON 存 set 里的元素）")
def _():
    keys = {common.knowledge_hint_key(p) for p in CARDED} | {"system"}
    assert all(isinstance(k, str) and k for k in keys)
    assert len(sorted(keys)) == len(keys)


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
