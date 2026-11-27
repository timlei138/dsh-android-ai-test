# -*- coding: utf-8 -*-
"""`SessionDB` 的知识卡键持久化单测（N-05 的存储侧）。

## 为什么单独测这一层

`common.knowledge_hint` 只负责"要不要给"，**给过没有是存在库里的**。
存储侧错了同样静默：键写不进去 → 每步都重发（token 白烧）；
键读不出来 → 永远当没提示过 → 同样每步重发；
`knowledge_shown` 旧列失真 → 老读法（webui / 旧会话）看到的口径变了。

用 `SessionDB(path)` 直接指向临时库，不碰真库、不改环境变量。
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


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def _tmp_parent() -> str:
    import tempfile as _t
    p = ROOT / "storage" / "tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    except OSError:
        return _t.gettempdir()


class _Fx:
    """临时库 + 一个会话。"""

    def __enter__(self):
        self._td = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.db = dbm.SessionDB(Path(self._td.name) / "test.db")
        self.sid = self.db.start_session("t", "u", run_id="fxrun")
        return self

    def __exit__(self, *exc):
        # ⚠️ Windows 上 SQLite 连接不关，临时目录就删不掉
        # （`PermissionError: [WinError 32] 另一个程序正在使用此文件`）。
        # SessionDB 用的是 threading.local 连接、没有 close() 方法，
        # 所以这里手动关掉再清。这不影响被测逻辑：每个 case 都是新库。
        for attr in ("db", "db2"):
            inst = getattr(self, attr, None)
            if inst is None:
                continue
            try:
                conn = getattr(inst._local, "conn", None)
                if conn is not None:
                    conn.close()
                    inst._local.conn = None
            except Exception:  # noqa: BLE001
                pass
        self._td.cleanup()
        return False


@case("新会话 → 键集合为空、旧标记为 False")
def _():
    with _Fx() as fx:
        assert fx.db.knowledge_hint_keys(fx.sid) == set()
        assert fx.db.knowledge_shown(fx.sid) is False


@case("mark 一个键 → 读回来就一个；旧的 knowledge_shown 同时置 True")
def _():
    with _Fx() as fx:
        fx.db.mark_knowledge_hint(fx.sid, "card:com.zui.calendar")
        assert fx.db.knowledge_hint_keys(fx.sid) == {"card:com.zui.calendar"}
        assert fx.db.knowledge_shown(fx.sid) is True


@case("★ 累加：mark 三个不同键 → 都在（旧的布尔标记放不下这个信息）")
def _():
    with _Fx() as fx:
        for k in ("card:com.zui.calendar", "card:android.settings", "system"):
            fx.db.mark_knowledge_hint(fx.sid, k)
        assert fx.db.knowledge_hint_keys(fx.sid) == {
            "card:com.zui.calendar", "card:android.settings", "system"}


@case("★ 重复 mark 同一个键 → 幂等，不重复、不炸")
def _():
    with _Fx() as fx:
        for _ in range(5):
            fx.db.mark_knowledge_hint(fx.sid, "card:com.zui.calendar")
        assert fx.db.knowledge_hint_keys(fx.sid) == {"card:com.zui.calendar"}


@case("空键 / 纯空白键 → 直接忽略（start 时包名为空就走到这里）")
def _():
    with _Fx() as fx:
        fx.db.mark_knowledge_hint(fx.sid, "")
        fx.db.mark_knowledge_hint(fx.sid, "   ")
        fx.db.mark_knowledge_hint(fx.sid, None)
        assert fx.db.knowledge_hint_keys(fx.sid) == set()
        assert fx.db.knowledge_shown(fx.sid) is False


@case("进程重开后仍在（是落盘字段，不是内存态）")
def _():
    with _Fx() as fx:
        fx.db.mark_knowledge_hint(fx.sid, "card:com.zui.calendar")
        db2 = dbm.SessionDB(Path(fx._td.name) / "test.db")
        fx.db2 = db2          # 交给 __exit__ 关闭，否则临时目录删不掉
        assert db2.knowledge_hint_keys(fx.sid) == {"card:com.zui.calendar"}


@case("★ 会话之间互不影响（一个会话提示过，不该让另一个会话静默）")
def _():
    with _Fx() as fx:
        sid2 = fx.db.start_session("t2", "u2", run_id="fxrun2")
        fx.db.mark_knowledge_hint(fx.sid, "card:com.zui.calendar")
        assert fx.db.knowledge_hint_keys(sid2) == set()


@case("不存在的会话号 → 返回空集合，不抛")
def _():
    with _Fx() as fx:
        assert fx.db.knowledge_hint_keys(999999) == set()
        assert fx.db.knowledge_shown(999999) is False
        fx.db.mark_knowledge_hint(999999, "card:x")  # 不该抛


@case("列值是坏 JSON / 非数组 → 当作空集合，不抛")
def _():
    with _Fx() as fx:
        conn = fx.db._conn()
        for bad in ("not json", "{}", "123", "null"):
            conn.execute("UPDATE sessions SET knowledge_hint_keys=? WHERE id=?",
                         (bad, fx.sid))
            conn.commit()
            assert fx.db.knowledge_hint_keys(fx.sid) == set(), bad


@case("列值里混了非字符串 → 统一转成字符串（回读不炸）")
def _():
    with _Fx() as fx:
        conn = fx.db._conn()
        conn.execute("UPDATE sessions SET knowledge_hint_keys=? WHERE id=?",
                     ('["a", 1, null]', fx.sid))
        conn.commit()
        got = fx.db.knowledge_hint_keys(fx.sid)
        assert "a" in got and "1" in got, got


@case("★ 旧会话（只有 knowledge_shown=1，没有键列）也能正常读")
def _():
    with _Fx() as fx:
        conn = fx.db._conn()
        conn.execute("UPDATE sessions SET knowledge_shown=1, knowledge_hint_keys=NULL"
                     " WHERE id=?", (fx.sid,))
        conn.commit()
        assert fx.db.knowledge_shown(fx.sid) is True
        assert fx.db.knowledge_hint_keys(fx.sid) == set()


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
