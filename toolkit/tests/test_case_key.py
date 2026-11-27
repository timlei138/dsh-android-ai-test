# -*- coding: utf-8 -*-
"""`tools/db.py` 的 `_case_key` —— 用例归一键的单测。

## 为什么这一条必须有测试

`_case_key` 决定「两次运行是不是同一条用例」。它归并过头，两条不同用例被
并成一组，结论一 PASS 一 FAIL 就报成「抖动」—— **假警**，会让人去查一个
不存在的不稳定用例；它归并得太松，同一条用例被拆成几组，真抖动就凑不齐
样本 —— **漏报**，坏了也发现不了。

两种错的性质不同，取舍是明确的：**漏报可以接受，假警不可接受**。
所以这里断言的重点是「不该认领的必须不认领」（例如编号相同但模块不同的
那几组：`启动应用_03` / `重要日倒数_03`）。

依赖真实用例目录 `assets/cases/com.zui.calendar/`：那是仓库里的固定资产，
不是运行产物，拿它当夹具是稳的。
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

import db as dbm  # noqa: E402

PKG = "com.zui.calendar"
CASES_DIR = ROOT / "assets" / "cases" / PKG
CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def key(title):
    return dbm._case_key(title, PKG)


@case("夹具齐全（缺用例目录就没法判归并，直接失败而不是静默跳过）")
def _():
    stems = dbm._case_stems(PKG)
    assert stems, f"找不到用例目录 {CASES_DIR}"
    assert len(stems) >= 50, f"用例数异常少：{len(stems)}"


@case("原样写完整用例名 → 认领自己")
def _():
    for stem in ("com.zui.calendar_新建课程表_176",
                 "com.zui.calendar_重要日倒数_03"):
        assert key(stem) == stem


@case("标题里带完整用例名（前面另有描述） → 仍认领")
def _():
    got = key("联想日历 跑一下 com.zui.calendar_新建课程表_176 这条")
    assert got == "com.zui.calendar_新建课程表_176"


@case("片段集合相等 → 认领（顺序无关）")
def _():
    # 标题片段 {新建课程表, 176} 与文件名片段去包名后完全相等
    got = key("176_新建课程表")
    assert got == "com.zui.calendar_新建课程表_176", got


@case("片段只是近似（多出无法对齐的描述词） → 退回原文键")
def _():
    # 「新建课程表检查」不是文件名的片段，集合既不相等也不构成子集 → 不认领。
    # 这正是「漏报可以接受」的取舍：认了就可能是另一条用例。
    got = key("日历_176 新建课程表检查")
    assert got == "com.zui.calendar#日历_176 新建课程表检查", got


@case("★ 编号相同、模块不同 → 绝不互相认领（制造假警的来源）")
def _():
    # 03：启动应用 / 重要日倒数 并存
    a = key("com.zui.calendar_启动应用_03")
    b = key("com.zui.calendar_重要日倒数_03")
    assert a != b, f"03 重号被并到一起：{a}"
    # 04 / 05 / 08 同样各有多条
    for num in ("04", "05", "08"):
        keys = set()
        for stem in dbm._case_stems(PKG):
            if stem.endswith("_" + num):
                keys.add(key(stem))
        assert len(keys) == len([s for s in dbm._case_stems(PKG)
                                 if s.endswith("_" + num)]), (
            f"编号 {num} 下的多条用例被并成了 {len(keys)} 组")


@case("★ 「重要日倒数」系列各编号必须各自独立")
def _():
    series = [s for s in dbm._case_stems(PKG) if "_重要日倒数_" in s]
    assert len(series) >= 6, f"重要日倒数系列条数异常：{len(series)}"
    keys = {s: key(s) for s in series}
    assert len(set(keys.values())) == len(series), keys


@case("只有编号、模块看不出来 → 退回原文键（宁漏报不错并）")
def _():
    got = key("com.zui.calendar_176")
    assert got == "com.zui.calendar#176", got


@case("编号带模块但与文件名的模块不同 → 不认领别的用例")
def _():
    # 「重要日倒数07」不该被认领成 _重要日倒数_03（后缀匹配会撞，这是踩过的坑）
    got = key("重要日倒数07")
    assert got != "com.zui.calendar_重要日倒数_03", got
    got = key("重要日倒数_07")
    assert got != "com.zui.calendar_重要日倒数_03", got


@case("完全无关的标题 → 退回原文键，且不同标题不并")
def _():
    a = key("随便写个诊断会话")
    b = key("另一个诊断会话")
    assert a.startswith("com.zui.calendar#")
    assert a != b


@case("空标题不炸")
def _():
    assert isinstance(key(""), str)
    assert isinstance(key(None), str)


@case("稳定：同一输入调两次结果相同")
def _():
    for title in ("com.zui.calendar_176", "日历_176 新建课程表检查", "重要日倒数07"):
        assert key(title) == key(title)


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
