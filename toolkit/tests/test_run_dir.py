# -*- coding: utf-8 -*-
"""`tools/db.py` 的 `run_dir_path` / `run_dir_missing` 单测。

## 为什么值得单独测

`sessions.run_id` 有两代格式，而**旧格式指向的目录全都不存在**（单库化
把 `run_history/<hex12>/db.sqlite3` 那套去掉了）。所有拼
`STORAGE_DIR/"run_history"/run_id` 的代码都还照着拼，拼出来是个不存在的
路径 —— 不报错，只是静默空转：回放空白、录屏收尾白等满 timeout。

所以「这个 run_id 的目录还在不在」成了一个要在几处共用的判断，它自己错了
（比如把 `..` 放过去、把空串也当合法路径）后果比不判更糟。这里只测纯逻辑，
用临时目录当夹具，不碰真库。
"""
import io
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

import db as dbm  # noqa: E402

CASES = []

def _tmp_parent() -> str:
    """临时目录的父目录：优先仓库内的 storage/tmp，不可用就退回系统临时目录。

    测试不该因为「从没跑过用例、storage/tmp 还没被建出来」就报错。
    """
    import tempfile
    p = ROOT / "storage" / "tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    except OSError:
        return tempfile.gettempdir()



def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


@case("空 run_id → 无路径，且算 missing")
def _():
    assert dbm.run_dir_path(None) is None
    assert dbm.run_dir_path("") is None
    assert dbm.run_dir_missing(None) is True
    assert dbm.run_dir_missing("") is True


@case("合法 run_id → 拼到 storage/run_history 下")
def _():
    p = dbm.run_dir_path("abc123")
    assert p is not None
    assert p.parts[-3:] == ("storage", "run_history", "abc123"), p


@case("uuid 形式合法")
def _():
    rid = "ba194707-8404-48f3-94a8-b0928fac8cd7"
    assert dbm.run_dir_path(rid) is not None


@case("★ 路径穿越必须拒绝（拼路径的人不会自己判 ../）")
def _():
    for bad in ("../etc", "a/../b", "a/b", "..", ".", "a\\b"):
        assert dbm.run_dir_path(bad) is None, f"没拦住 {bad!r}"
        assert dbm.run_dir_missing(bad) is True


@case("超长 run_id 拒绝")
def _():
    assert dbm.run_dir_path("x" * 81) is None
    assert dbm.run_dir_path("x" * 80) is not None


@case("不存在 → missing=True；建出来后 → missing=False（实时读盘，不缓存）")
def _():
    with tempfile.TemporaryDirectory(dir=_tmp_parent()) as td:
        rid = "testrun12345"
        fake_root = Path(td)
        saved = dbm.STORAGE_DIR
        try:
            dbm.STORAGE_DIR = fake_root
            assert dbm.run_dir_missing(rid) is True
            (fake_root / "run_history" / rid).mkdir(parents=True)
            assert dbm.run_dir_missing(rid) is False
            (fake_root / "run_history" / rid).rmdir()
            assert dbm.run_dir_missing(rid) is True
        finally:
            dbm.STORAGE_DIR = saved


@case("同名但它是文件，不是目录 → 仍算 missing")
def _():
    with tempfile.TemporaryDirectory(dir=_tmp_parent()) as td:
        rid = "notadir12345"
        fake_root = Path(td)
        (fake_root / "run_history").mkdir(parents=True)
        (fake_root / "run_history" / rid).write_text("x", encoding="utf-8")
        saved = dbm.STORAGE_DIR
        try:
            dbm.STORAGE_DIR = fake_root
            assert dbm.run_dir_missing(rid) is True
        finally:
            dbm.STORAGE_DIR = saved


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
