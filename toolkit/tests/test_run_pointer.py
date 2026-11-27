# -*- coding: utf-8 -*-
"""`tools/runtime.py` 的运行目录指针（`.current` / `.last`）单测。

## 为什么必须测（N-02 / N-33，都是真实事故）

**N-33：`last_record_dir()` 判据失效。** 它原来读 `.last` 之后要确认目录里有
`db.sqlite3` 才认。库在单库化之后搬到了 `storage/test.db`，运行目录里**再也
没有** `db.sqlite3` —— 函数恒返回 None（实测 298 个运行目录 0 个含该文件），
功能静默死亡：不报错、不告警，只是"上一次的运行目录永远查不到"。
修法与 `_pinned_record()` 同口径：**目录还在就算数**。

**N-02：`.current` 没有归属校验。** A 会话还在跑，B 会话 unpin 会把 A 的
指针一起清掉 → A 的下一张截图落到 `storage/evidence/_orphan/`，而
`current_session()` 仍是活的。真库 `_orphan/` 10 个目录、`orphan_evidence.log`
31 行。修法：`unpin_record(owner)` 只在 owner 就是当前指针时才算数。

这两个函数只碰文件系统，不碰 adb、不碰数据库 —— 用临时目录就能测。
⚠️ `runtime` 的 `STORAGE_DIR` 在 import 时就定死了（读环境变量），所以这里
用 `tempfile.TemporaryDirectory` 建独立目录后**直接改模块常量**，测完还原。
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

import runtime as RT  # noqa: E402

CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def _tmp_parent() -> str:
    """临时目录的父目录：优先仓库内的 storage/tmp，不可用就退回系统临时目录。"""
    p = ROOT / "storage" / "tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    except OSError:
        return tempfile.gettempdir()


class _Sandbox:
    """把 runtime 的 RECORD_DIR / 两个指针常量临时指到一个空目录。"""

    def __enter__(self):
        self._saved = (RT.RECORD_DIR, RT._CURRENT_POINTER, RT._LAST_POINTER)
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.root = Path(self._tmp.name)
        RT.RECORD_DIR = self.root
        RT._CURRENT_POINTER = self.root / ".current"
        RT._LAST_POINTER = self.root / ".last"
        return self

    def __exit__(self, *exc):
        RT.RECORD_DIR, RT._CURRENT_POINTER, RT._LAST_POINTER = self._saved
        self._tmp.cleanup()
        return False

    def run_dir(self, name: str) -> Path:
        p = self.root / name
        p.mkdir(parents=True, exist_ok=True)
        return p


# ── N-33：last_record_dir ────────────────────────────────────────────────


@case("★ last_record_dir：目录存在但没有 db.sqlite3 → 仍然认得（N-33 的原缺陷）")
def _():
    with _Sandbox() as box:
        target = box.run_dir("run-a")
        assert not (target / "db.sqlite3").exists(), "夹具不该有 db.sqlite3"
        box._saved and None
        RT._LAST_POINTER.write_text(str(target), encoding="utf-8")
        got = RT.last_record_dir()
        assert got == target.resolve(), (got, target)


@case("last_record_dir：目录不在了 → None（不能返回一个不存在的路径）")
def _():
    with _Sandbox() as box:
        gone = box.root / "run-gone"
        RT._LAST_POINTER.write_text(str(gone), encoding="utf-8")
        assert RT.last_record_dir() is None


@case("last_record_dir：指针文件不存在 / 空内容 → None，不抛")
def _():
    with _Sandbox():
        assert RT.last_record_dir() is None
        RT._LAST_POINTER.write_text("", encoding="utf-8")
        assert RT.last_record_dir() is None
        RT._LAST_POINTER.write_text("   \n", encoding="utf-8")
        assert RT.last_record_dir() is None


@case("last_record_dir：指针带 BOM 也能读（PowerShell Set-Content 会写 BOM）")
def _():
    with _Sandbox() as box:
        target = box.run_dir("run-bom")
        RT._LAST_POINTER.write_bytes(b"\xef\xbb\xbf" + str(target).encode("utf-8"))
        assert RT.last_record_dir() == target.resolve()


@case("last_record_dir：指针指向的是文件不是目录 → None")
def _():
    with _Sandbox() as box:
        f = box.root / "not-a-dir"
        f.write_text("x", encoding="utf-8")
        RT._LAST_POINTER.write_text(str(f), encoding="utf-8")
        assert RT.last_record_dir() is None


# ── N-02：pin / unpin 归属 ────────────────────────────────────────────────


@case("pin：写了 .current，_pinned_record 能读回来")
def _():
    with _Sandbox() as box:
        a = box.run_dir("run-a")
        RT.pin_record(a)
        assert RT._CURRENT_POINTER.exists()
        assert RT._pinned_record() == a.resolve()


@case("★ unpin：owner 不匹配 → 不清别人的指针（N-02 的原缺陷）")
def _():
    with _Sandbox() as box:
        a = box.run_dir("run-a")
        b = box.run_dir("run-b")
        RT.pin_record(a)
        removed = RT.unpin_record(b)          # B 试图清 A 的
        assert removed is False, "不该清掉别人的指针"
        assert RT._CURRENT_POINTER.exists(), "指针被无辜清掉了"
        assert RT._pinned_record() == a.resolve()


@case("unpin：owner 匹配 → 清指针，并把路径写进 .last")
def _():
    with _Sandbox() as box:
        a = box.run_dir("run-a")
        RT.pin_record(a)
        assert RT.unpin_record(a) is True
        assert not RT._CURRENT_POINTER.exists()
        assert RT._LAST_POINTER.exists()
        # .last 的用途就是给收尾后回查 —— 必须和刚被 unpin 的那个目录一致
        assert RT.last_record_dir() == a.resolve()


@case("unpin：没有 pin 过 → 返回 False，不抛")
def _():
    with _Sandbox():
        assert RT.unpin_record(None) is False
        assert RT.unpin_record("some-other-run") is False


@case("★ 所有调用点都必须传 owner（否则归属校验形同虚设）")
def _():
    # `unpin_record(None)` 是刻意保留的**无校验兼容模式**（见它的 docstring），
    # 单测无法从行为上拦住"有人又写成 None"。所以这里直接查源码：
    # 生产代码里每个调用点都必须给 owner，`tests/` 自己怎么调不管。
    import re
    offenders = []
    for path in sorted((ROOT / "tools").glob("*.py")):
        text = path.read_text(encoding="utf-8", newline="")
        for m in re.finditer(r"unpin_record\(([^)]*)\)", text):
            arg = m.group(1).strip()
            if arg not in ("", "None"):
                continue
            # 定义处（def unpin_record(owner...)= "") 与 docstring 里的示例不算
            line_start = text.rfind("\n", 0, m.start()) + 1
            line = text[line_start:text.find("\n", m.start())]
            if "def unpin_record" in line or "#" in line.split("unpin_record")[0]:
                continue
            offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, "这些调用点没传 owner：\n    " + "\n    ".join(offenders)


@case("pin 覆盖：连续 pin 两个目录，指针指向后一个")
def _():
    with _Sandbox() as box:
        a = box.run_dir("run-a")
        b = box.run_dir("run-b")
        RT.pin_record(a)
        RT.pin_record(b)
        assert RT._pinned_record() == b.resolve()


@case("unpin 之后 pin 另一个：.last 保留上一个，.current 指新的")
def _():
    with _Sandbox() as box:
        a = box.run_dir("run-a")
        b = box.run_dir("run-b")
        RT.pin_record(a)
        RT.unpin_record(a)
        RT.pin_record(b)
        assert RT._pinned_record() == b.resolve()
        assert RT.last_record_dir() == a.resolve()


def main() -> int:
    print("=" * 70)
    print("tools/runtime.py —— 运行目录指针")
    print("=" * 70)
    failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as error:
            failed += 1
            print(f"  [FAIL] {name}  {error}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}  异常：{type(error).__name__}: {error}")
        else:
            print(f"  [PASS] {name}")
    print(f"\n  {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
