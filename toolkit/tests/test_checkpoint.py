# -*- coding: utf-8 -*-
"""`tools/checkpoint.py` —— 检查点解析与判定的单测。

这两件事都**不能靠真机验**：parse 是纯字符串，evaluate 是纯比较。
它们错了的后果还特别严重 ——
  · parse 静默丢弃条件 → 「我写了检查点」和「我没写」在结果上一模一样，
    检查永远通过（这是真实缺陷，见 `--expect TaskId=xxx`）；
  · evaluate 用 `endswith` 裸匹配 → `EditTimetableActivity` 会命中
    `TimetableActivity`，检查在**错误的页面**上静默通过。
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

import checkpoint  # noqa: E402

CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def expect_raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except checkpoint.ExpectSpecError:
        return True
    return False


@case("parse：空/None → 空条件（表示不校验）")
def _():
    assert checkpoint.parse_expect(None) == {}
    assert checkpoint.parse_expect("") == {}
    assert checkpoint.parse_expect("   ") == {}


@case("parse：单条件与多条件 AND")
def _():
    assert checkpoint.parse_expect("rid=btn_ok") == {"rid": ["btn_ok"]}
    assert checkpoint.parse_expect("activity=A,rid=btn_ok") == {
        "activity": ["A"], "rid": ["btn_ok"]}


@case("parse：竖线 = 任一")
def _():
    assert checkpoint.parse_expect("activity=A|B") == {"activity": ["A", "B"]}


@case("parse：未知键必须报错，不能静默丢弃")
def _():
    assert expect_raises(checkpoint.parse_expect, "TaskId=xxx")
    assert expect_raises(checkpoint.parse_expect, "activity=A,TaskId=1")


@case("parse：缺 = / 空值 / 空键 都要报错")
def _():
    assert expect_raises(checkpoint.parse_expect, "activity")
    assert expect_raises(checkpoint.parse_expect, "activity=")
    assert expect_raises(checkpoint.parse_expect, "=A")
    assert expect_raises(checkpoint.parse_expect, "activity=|")


@case("parse：报错文案要能直接照抄认错")
def _():
    try:
        checkpoint.parse_expect("TaskId=xxx")
    except checkpoint.ExpectSpecError as exc:
        text = str(exc)
        assert "TaskId" in text
        assert "activity/rid/text/desc" in text
        assert "--expect" in text
    else:
        raise AssertionError("没有抛错")


@case("evaluate：没写检查点 → checked=False（不等于通过）")
def _():
    got = checkpoint.evaluate({}, activity="AnyActivity", sig=[])
    assert got["checked"] is False
    assert got["ok"] is True          # 兼容旧调用方，但 checked 必须为 False
    assert got["matched"] == [] and got["missed"] == []


@case("evaluate：activity 按段落边界，EditTimetableActivity 不算命中 TimetableActivity")
def _():
    got = checkpoint.evaluate({"activity": ["TimetableActivity"]},
                              activity=".EditTimetableActivity", sig=[])
    assert got["checked"] is True
    assert got["ok"] is False, "EditTimetableActivity 不该命中 TimetableActivity"

    got = checkpoint.evaluate({"activity": ["TimetableActivity"]},
                              activity=".TimetableActivity", sig=[])
    assert got["ok"] is True

    got = checkpoint.evaluate({"activity": ["TimetableActivity"]},
                              activity="com.zui.calendar.TimetableActivity", sig=[])
    assert got["ok"] is True, "完整类名段应命中短名"


@case("evaluate：rid/text/desc 走 sig 集合，精确成员")
def _():
    sig = ["rid=btn_ok", "text=完成", "desc=关闭"]
    assert checkpoint.evaluate({"rid": ["btn_ok"]}, activity="", sig=sig)["ok"] is True
    assert checkpoint.evaluate({"rid": ["btn_ok2"]}, activity="", sig=sig)["ok"] is False
    # 短前缀不能误命中（sig 是「key=value」整串比较，不是 startswith）
    assert checkpoint.evaluate({"text": ["完"]}, activity="", sig=sig)["ok"] is False


@case("evaluate：竖线任一，命中一个就算过")
def _():
    sig = ["rid=btn_cancel"]
    got = checkpoint.evaluate({"rid": ["btn_ok", "btn_cancel"]}, activity="", sig=sig)
    assert got["ok"] is True


@case("evaluate：AND 多条件，缺一个就不通过并列出 missed")
def _():
    sig = ["rid=btn_ok"]
    got = checkpoint.evaluate({"activity": ["A"], "rid": ["btn_ok"]},
                              activity=".A", sig=sig)
    assert got["ok"] is True
    got = checkpoint.evaluate({"activity": ["B"], "rid": ["btn_ok"]},
                              activity=".A", sig=sig)
    assert got["ok"] is False
    assert len(got["missed"]) == 1 and "activity" in got["missed"][0]


@case("evaluate：activity 为空时任何 activity 条件都不命中")
def _():
    got = checkpoint.evaluate({"activity": ["A"]}, activity=None, sig=[])
    assert got["ok"] is False


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
