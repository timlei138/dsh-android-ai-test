# -*- coding: utf-8 -*-
"""`tools/db.py` 三个查询函数 —— 都是「修复前静默给错数据」的坑。

## 为什么（真实数据）

- `list_events`（N-24）：旧版只 `SELECT seq, kind, tool, detail, data_json`，
  **拿不到事件主键 / 证据路径 / 耗时** —— 任何「按事件回查证据」的调用方
  都得再查一遍库，或者干脆查不出来。导出台账、复盘、定位某一步的截图全卡在这。
- `efficiency_stats`（N-26）：旧版靠 `'"type": "swipe"' in data_json` 判滑动。
  真库 4000 条 act 事件里 `json.loads` 数出 **436 次 swipe**，字符串匹配得 **0**
  —— 因为写库用的是压缩风格 JSON（`separators=(",", ":")`）。效率自查于是
  永远是「没问题」，等于没查。
- `list_runs`（N-25）：旧版按 `PASS, FAIL, BLOCKED, ERROR` 找第一个命中 →
  「同一次运行一条 PASS 一条 FAIL」显示成 **PASS**，把失败盖掉；`running`
  没进优先级；标题用 `GROUP_CONCAT` 后 `split(",")[0]`，而**用例标题里本来
  就有逗号**（「日历_176 新建课程表，检查空态」）→ 标题被截断。

全部用**临时库**（`SessionDB(临时路径)`），不碰真库。
⚠️ `SessionDB` 没有 `close()`，Windows 上不关连接会让临时目录删不掉。
"""
import io
import json
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
    p = ROOT / "storage" / "tmp"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return str(p)
    except OSError:
        return tempfile.gettempdir()


class _Fx:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.db = dbm.SessionDB(Path(self._tmp.name) / "test.db")
        return self

    def __exit__(self, *exc):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.db._local.conn = None
        self._tmp.cleanup()
        return False

    def session(self, title: str = "t", run_id: str | None = "run-x") -> int:
        return self.db.start_session(title=title, user_input="", device="fake",
                                     run_id=run_id or "placeholder")


# ---------------------------------------------------------------- list_events

@case("★ list_events 带出 id / evidence / duration_ms（N-24：旧版这四列都没有）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        eid = fx.db.log_event(sid, "act", "tap", "点了", {"action": {"type": "tap"}},
                              evidence="run_history/r/evidence/1/step01/shot.png",
                              duration_ms=123,
                              started_at="2026-11-26T10:00:00.000")
        rows = fx.db.list_events(sid)
        assert len(rows) == 1, rows
        r = rows[0]
        for key in ("id", "seq", "kind", "tool", "detail", "data_json",
                    "evidence", "duration_ms", "started_at", "created_at", "call_id"):
            assert key in r, f"缺字段 {key}：{sorted(r)}"
        assert r["id"] == eid, (r["id"], eid)
        assert r["evidence"] == "run_history/r/evidence/1/step01/shot.png"
        assert r["duration_ms"] == 123
        assert r["started_at"] == "2026-11-26T10:00:00.000"


@case("list_events 按 seq 升序，且 data_json 是原文（没被反序列化）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        for i in range(3):
            fx.db.log_event(sid, "act", "tap", f"第{i}下", {"action": {"type": "tap"}})
        rows = fx.db.list_events(sid)
        assert [r["seq"] for r in rows] == [1, 2, 3], [r["seq"] for r in rows]
        assert isinstance(rows[0]["data_json"], str)
        assert json.loads(rows[0]["data_json"])["action"]["type"] == "tap"


@case("list_events 不串会话；空会话返回 []")
def _():
    with _Fx() as fx:
        a, b = fx.session("a"), fx.session("b")
        fx.db.log_event(a, "act", "tap", "在 a")
        assert len(fx.db.list_events(a)) == 1
        assert fx.db.list_events(b) == []
        assert fx.db.list_events(999999) == []


# ---------------------------------------------------------- efficiency_stats

@case("★ efficiency_stats 必须真数出 swipe（N-26：压缩风格 JSON 下旧版得 0）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        # 关键：用压缩分隔符写库 —— 这正是真库的实际写法，也是旧版字符串匹配
        # 失效的原因（`'"type": "swipe"'` 带空格，压缩后是 `'"type":"swipe"'`）。
        for _ in range(3):
            fx.db.log_event(sid, "act", "swipe", "滑",
                            {"action": {"type": "swipe"}})
        stats = fx.db.efficiency_stats(sid)
        assert stats["swipe"] == 3, stats


@case("multi_stage_swipe / drag 也算滑动（不能只认 'swipe'）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        for kind in ("swipe", "multi_stage_swipe", "drag", "tap", "input"):
            fx.db.log_event(sid, "act", "act", kind, {"action": {"type": kind}})
        stats = fx.db.efficiency_stats(sid)
        assert stats["swipe"] == 3, stats
        assert stats["act"] == 5, stats


@case("★ cp_miss 按 checkpoint.ok is False 数（字符串匹配同样数不到）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        fx.db.log_event(sid, "act", "tap", "命中", {"action": {"type": "tap"},
                                                   "checkpoint": {"ok": True}})
        for _ in range(2):
            fx.db.log_event(sid, "act", "tap", "没命中",
                            {"action": {"type": "tap"},
                             "checkpoint": {"ok": False, "missed": ["rid=x"]}})
        stats = fx.db.efficiency_stats(sid)
        assert stats["cp_miss"] == 2, stats


@case("ratio = observe / act；没有 act 时是 None（不除零）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        for _ in range(4):
            fx.db.log_event(sid, "act", "tap", "点", {"action": {"type": "tap"}})
        for _ in range(2):
            fx.db.log_event(sid, "observe", "observe", "看")
        stats = fx.db.efficiency_stats(sid)
        assert stats["act"] == 4 and stats["observe"] == 2, stats
        assert stats["ratio"] == 0.5, stats

        sid2 = fx.session("只有观察")
        fx.db.log_event(sid2, "observe", "observe", "看")
        assert fx.db.efficiency_stats(sid2)["ratio"] is None


@case("坏 data_json / 非 dict JSON 不让整轮统计炸掉")
def _():
    with _Fx() as fx:
        sid = fx.session()
        fx.db._conn().execute(
            "INSERT INTO events (session_id, seq, kind, tool, detail, data_json,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (sid, 1, "act", "tap", "", "not-json{{{", "2026-11-26T10:00:00"))
        fx.db._conn().execute(
            "INSERT INTO events (session_id, seq, kind, tool, detail, data_json,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (sid, 2, "act", "tap", "", "[1,2,3]", "2026-11-26T10:00:01"))
        fx.db._conn().commit()
        stats = fx.db.efficiency_stats(sid)
        assert stats["act"] == 2 and stats["swipe"] == 0, stats


# ------------------------------------------------------------------ list_runs

@case("★ 同一 run 有 running → 整次运行显示 running（N-25：旧版会显示 FAIL/PASS）")
def _():
    with _Fx() as fx:
        for status in ("PASS", "FAIL"):
            sid = fx.session(f"s-{status}", run_id="run-multi")
            fx.db.log_event(sid, "read", "observe", "看")
            fx.db._conn().execute("UPDATE sessions SET status=? WHERE id=?",
                                  (status, sid))
        sid = fx.session("s-running", run_id="run-multi")
        fx.db._conn().commit()
        runs = {r["run_id"]: r for r in fx.db.list_runs(limit=50)}
        assert runs["run-multi"]["status"] == "running", runs["run-multi"]


@case("★ 没有 running 时取最坏结论 ERROR > BLOCKED > FAIL > PASS（旧版会把 PASS 排前面）")
def _():
    with _Fx() as fx:
        for status in ("PASS", "FAIL", "ERROR", "BLOCKED"):
            sid = fx.session(f"s-{status}", run_id="run-worst")
            fx.db._conn().execute("UPDATE sessions SET status=? WHERE id=?",
                                  (status, sid))
        fx.db._conn().commit()
        runs = {r["run_id"]: r for r in fx.db.list_runs(limit=50)}
        assert runs["run-worst"]["status"] == "ERROR", runs["run-worst"]


@case("只 PASS 才显示 PASS（不能把没跑完的当成功）")
def _():
    with _Fx() as fx:
        sid = fx.session("ok", run_id="run-pass")
        fx.db._conn().execute("UPDATE sessions SET status='PASS' WHERE id=?", (sid,))
        fx.db._conn().commit()
        runs = {r["run_id"]: r for r in fx.db.list_runs(limit=50)}
        assert runs["run-pass"]["status"] == "PASS"


@case("★ 标题里的逗号不能被截断（N-25：旧版 split(',')[0]）")
def _():
    title = "日历_176 新建课程表，检查空态后的表现，含子场景"
    with _Fx() as fx:
        sid = fx.session(title, run_id="run-title")
        fx.db._conn().execute("UPDATE sessions SET status='PASS' WHERE id=?", (sid,))
        fx.db._conn().commit()
        runs = {r["run_id"]: r for r in fx.db.list_runs(limit=50)}
        assert runs["run-title"]["title"] == title, runs["run-title"]["title"]


@case("statuses 仍然给出全部结论（便于区分「真只跑了一条」与「多条同结论」）")
def _():
    with _Fx() as fx:
        for status in ("PASS", "FAIL"):
            sid = fx.session(f"s-{status}", run_id="run-sts")
            fx.db._conn().execute("UPDATE sessions SET status=? WHERE id=?",
                                  (status, sid))
        fx.db._conn().commit()
        runs = {r["run_id"]: r for r in fx.db.list_runs(limit=50)}
        assert runs["run-sts"]["statuses"] == ["FAIL", "PASS"], runs["run-sts"]


@case("stale_run_id：run 目录不存在时为 True（旧 hex12 记录）")
def _():
    with _Fx() as fx:
        sid = fx.session("old", run_id="deadbeef0001")
        fx.db._conn().execute("UPDATE sessions SET status='PASS' WHERE id=?", (sid,))
        fx.db._conn().commit()
        runs = {r["run_id"]: r for r in fx.db.list_runs(limit=50)}
        assert runs["deadbeef0001"]["stale_run_id"] is True, runs["deadbeef0001"]


@case("run_id 为空的会话不进运行列表（不造出空 run）")
def _():
    with _Fx() as fx:
        fx.db._conn().execute(
            "INSERT INTO sessions (run_id, title, user_input, device, kind,"
            " started_at) VALUES (NULL,'无 run','','fake','test','2026-11-26T10:00:00')")
        fx.db._conn().commit()
        assert fx.db.list_runs(limit=50) == [], fx.db.list_runs(limit=50)


def main() -> int:
    print("=" * 70)
    print("tools/db.py —— list_events / efficiency_stats / list_runs")
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
