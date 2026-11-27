# -*- coding: utf-8 -*-
"""`tools/session.py` 的 `export_steps()` —— `_steps.yaml` 的头部字段。

## 为什么（N-34，真实数据）

导出的 `_steps.yaml` 里有一行 `source_run:`，本意是「这点路径是哪一次运行
实测出来的」。原来写的是

    f"source_run: {Path(str(db.path)).parent.name}"

`db.path` 是 `storage/test.db`，它的父目录名恒等于 `"storage"` —— 于是
**57/57 个真实 `_steps.yaml` 全是 `source_run: storage`**，这个字段零信息量。
修法：用这一轮的 `run_id`（`storage/run_history/<run_id>` 里那个 uuid）。

顺带把 N-06 的静默返回也钉住：解析不出用例 md 时，`export_steps` 返回
`(None, 原因)`，原因必须是非空中文 —— 原来直接 `return None`，调用方分不清
「这次没什么可复用的」和「导出炸了」。

用的是**临时库 + 临时用例目录**（`SessionDB(临时路径)` + 改 `CASES_DIR`），
不碰真库、不碰真用例目录。
⚠️ `SessionDB` 没有 `close()`，Windows 上不关连接会让临时目录删不掉，
所以 fixture 里手动关 `_local.conn`（同 `test_knowledge_keys_db.py`）。
"""
import io
import shutil
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

import common  # noqa: E402
import db as dbm  # noqa: E402
import session as sess  # noqa: E402

CASES = []

PKG = "com.zui.calendar"
STEM = f"{PKG}_新建课程表_176"


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def _tmp_parent() -> str:
    p = ROOT / "storage" / "tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    except OSError:
        return tempfile.gettempdir()


class _Fx:
    """临时库 + 临时用例目录；退出时关连接、还原 CASES_DIR。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.root = Path(self._tmp.name)
        self.cases = self.root / "cases" / PKG
        self.cases.mkdir(parents=True)
        src = ROOT / "assets" / "cases" / PKG / f"{STEM}.md"
        shutil.copy2(src, self.cases / f"{STEM}.md")

        self._saved = (sess.CASES_DIR, common.CASES_DIR)
        sess.CASES_DIR = self.root / "cases"
        common.CASES_DIR = self.root / "cases"

        self.db = dbm.SessionDB(self.root / "test.db")
        return self

    def __exit__(self, *exc):
        for attr in ("db",):
            inst = getattr(self, attr, None)
            if inst is None:
                continue
            conn = getattr(inst._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                inst._local.conn = None
        sess.CASES_DIR, common.CASES_DIR = self._saved
        self._tmp.cleanup()
        return False

    def new_session(self, *, title: str, run_id: str | None,
                    package: str | None = PKG) -> int:
        sid = self.db.start_session(title=title, user_input=f"测试用例 {title}",
                                    device="fake", run_id=run_id or "placeholder")
        if package:
            self.db.set_package(sid, package)
        if run_id is None:
            # start_session 不传 run_id 时会自动取当前运行目录名 —— 这里要的是
            # 「没有 run_id」的场景，直接把列改成 NULL
            self.db._conn().execute(
                "UPDATE sessions SET run_id=NULL WHERE id=?", (sid,))
            self.db._conn().commit()
        return sid

    def add_tap(self, sid: int, *, activity: str, via: str, expect: str,
                why: str = "用例步骤2：点进去") -> None:
        self.db.log_event(sid, "read", "observe", "起点",
                          {"package": PKG, "activity": activity, "sig": []})
        self.db.log_event(sid, "act", "tap", "点击",
                          {"package": PKG, "activity": activity,
                           "sig": [via],
                           "action": {"type": "tap", "via": via,
                                      "expect": expect, "why": why}})
        self.db.log_event(sid, "read", "observe", "落点",
                          {"package": PKG, "activity": expect.split("=", 1)[-1],
                           "sig": []})

    def read_yaml(self, path: str | None) -> str:
        # 返回的是**仓库相对路径**（`assets/cases/<pkg>/xxx_steps.yaml`，
        # 给 WebUI 点开用的）。这个 fixture 把 CASES_DIR 换成了临时目录，
        # 所以按文件名到临时目录里取实际写出来的那份。
        assert path, "export_steps 返回了 None"
        real = self.cases / Path(path).name
        assert real.is_file(), f"返回了 {path}，但临时目录里没有 {real}"
        return real.read_text(encoding="utf-8")


@case("★ source_run 必须是 run_id，不能是恒定的 'storage'（N-34 的原缺陷）")
def _():
    run_id = "a1b2c3d4-1111-2222-3333-444455556666"
    with _Fx() as fx:
        sid = fx.new_session(title="日历_176 新建课程表", run_id=run_id)
        fx.add_tap(sid, activity="com.zui.calendar.AllInOneActivity",
                   via="rid=btnAdd", expect="activity=EditTimetableActivity")
        path, skip = sess.export_steps(fx.db, sid)
        assert not skip, skip
        text = fx.read_yaml(path)
        assert f"source_run: {run_id}" in text, text[:400]
        assert "source_run: storage" not in text, "还是写成了固定值"


@case("source_run 与实际 run_id 一致（不能写死、也不能写别的会话的）")
def _():
    with _Fx() as fx:
        a = fx.new_session(title="日历_176 新建课程表", run_id="run-aaa-111")
        b = fx.new_session(title="日历_176 新建课程表", run_id="run-bbb-222")
        fx.add_tap(a, activity="com.zui.calendar.AllInOneActivity",
                   via="rid=btnAdd", expect="activity=EditTimetableActivity")
        fx.add_tap(b, activity="com.zui.calendar.AllInOneActivity",
                   via="rid=btnAdd", expect="activity=EditTimetableActivity")
        pa, _ = sess.export_steps(fx.db, a)
        assert "source_run: run-aaa-111" in fx.read_yaml(pa)
        pb, _ = sess.export_steps(fx.db, b)
        assert "source_run: run-bbb-222" in fx.read_yaml(pb)


@case("run_id 为空 → source_run 写 unknown（不留空值，yaml 不能破）")
def _():
    with _Fx() as fx:
        sid = fx.new_session(title="日历_176 新建课程表", run_id=None)
        fx.add_tap(sid, activity="com.zui.calendar.AllInOneActivity",
                   via="rid=btnAdd", expect="activity=EditTimetableActivity")
        path, skip = sess.export_steps(fx.db, sid)
        assert not skip, skip
        text = fx.read_yaml(path)
        assert "source_run: unknown" in text, text[:400]


@case("yaml 其它头部字段仍正确（case / verdict / step_count / start_from / steps）")
def _():
    with _Fx() as fx:
        sid = fx.new_session(title="日历_176 新建课程表", run_id="run-xyz")
        fx.add_tap(sid, activity="com.zui.calendar.AllInOneActivity",
                   via="rid=btnAdd", expect="activity=EditTimetableActivity",
                   why="用例步骤2：点击手动创建课程表")
        path, _ = sess.export_steps(fx.db, sid)
        text = fx.read_yaml(path)
        assert f"case: {STEM}" in text, text
        assert "step_count: 1" in text, text
        assert "start_from: activity=com.zui.calendar.AllInOneActivity" in text, text
        assert "    via: rid=btnAdd" in text, text
        assert "    expect: activity=EditTimetableActivity" in text, text
        assert "用例步骤2：点击手动创建课程表" in text, text


@case("★ N-06：解析不出用例 md → (None, 非空中文原因)，不能静默 None")
def _():
    with _Fx() as fx:
        sid = fx.new_session(title="完全不存在的用例名 XYZ", run_id="run-xyz")
        fx.add_tap(sid, activity="com.zui.calendar.AllInOneActivity",
                   via="rid=btnAdd", expect="activity=EditTimetableActivity")
        path, skip = sess.export_steps(fx.db, sid)
        assert path is None, path
        assert skip and skip.strip(), f"原因不能为空：{skip!r}"
        assert "md" in skip or "对不上" in skip, skip


@case("★ N-06：没有可复用步骤 → (None, 原因写着「不是出错」)")
def _():
    with _Fx() as fx:
        sid = fx.new_session(title="日历_176 新建课程表", run_id="run-xyz")
        fx.db.log_event(sid, "read", "observe", "只有观察，没有动作",
                        {"package": PKG,
                         "activity": "com.zui.calendar.AllInOneActivity", "sig": []})
        path, skip = sess.export_steps(fx.db, sid)
        assert path is None, path
        assert "不是出错" in skip, skip


@case("★ N-06：会话没有包名 → (None, 原因点名「包名」)")
def _():
    with _Fx() as fx:
        sid = fx.new_session(title="日历_176 新建课程表", run_id="run-xyz",
                             package=None)
        path, skip = sess.export_steps(fx.db, sid)
        assert path is None, path
        assert "包名" in skip, skip


@case("不存在的会话号 → 退回「没有包名」原因，不抛")
def _():
    with _Fx() as fx:
        path, skip = sess.export_steps(fx.db, 999999)
        assert path is None
        assert isinstance(skip, str) and skip.strip()


def main() -> int:
    print("=" * 70)
    print("tools/session.py —— export_steps()")
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
