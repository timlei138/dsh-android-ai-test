# -*- coding: utf-8 -*-
"""`session.py check` / `checklist` —— 「当前证据 ↔ 当前预期」的勾选闭环。

## 为什么（用户 2026-11-27 裁决）

> 「我们要保证的是当前的证据和当前的预期对上。必须是准确的。」

真库量化出来的病根：**多断言会话里 89.2%（354/397）的 finding 是会话结束那一刻
一次性写进去的**，写的时候手上没有「当帧」，只能从历史事件里挑看起来最像的那一张。
#619 / #1169 就是这么出的假 PASS —— 拿「点击之后」的帧去证「进入即应显示」。

修法不是加表、不是加列（用户明确否掉「再建一个 expect_items 库」）：
- 清单就是**用例原文的「预期结果」段**，用现成的 `common.case_expectations` 现场拆；
- 「勾没勾」= `findings` 里有没有 `expect_id` 指向它；
- `check` 子命令**不提供 `--event`**，证据自动绑「最后一条带证据的事件」，
  所以从设计上就没法回头挑一帧更有利的旧图；
- 两次勾选之间**必须产生新的看/动事件**（比较 seq），禁止会话末尾集中补勾。

本文件只测这些**结构性**约束，不测文案。用的是临时库 + 临时证据文件，
不碰真库。
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

import common  # noqa: E402
import db as dbm  # noqa: E402
import session as sess  # noqa: E402

CASES = []
PKG = "com.zui.calendar"
STEM = f"{PKG}_新建课程表_176"
MD = ROOT / "assets" / "cases" / PKG / f"{STEM}.md"


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
    """临时库。`user_input` 直接放用例原文 —— 清单就是从它里面拆出来的。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(dir=_tmp_parent())
        self.root = Path(self._tmp.name)
        self.text = MD.read_text(encoding="utf-8")
        self.db = dbm.SessionDB(self.root / "test.db")
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

    def session(self, case_title: str = "") -> int:
        """建一个带用例原文的会话（清单能拆出来）。"""
        sid = self.db.start_session(title=case_title or STEM, user_input=self.text,
                                    device="fake", run_id="run-checklist-test")
        self.db.set_package(sid, PKG)
        return sid

    def bare_session(self) -> int:
        """没有用例原文的会话 —— 拆不出清单。"""
        sid = self.db.start_session(title="随手记", user_input="随便跑跑",
                                    device="fake", run_id="run-bare")
        self.db.set_package(sid, PKG)
        return sid

    def frame(self, sid: int, name: str = "step01") -> int:
        """造一条带证据的事件（png 真存在，add_finding 会校验文件）。"""
        step = self.root / "evidence" / str(sid) / name
        step.mkdir(parents=True, exist_ok=True)
        shot = step / "shot.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        rel = f"storage/tmp/{self.root.name}/evidence/{sid}/{name}/shot.png"
        ev = self.db.log_event(sid, "observe", "observe", "看一眼",
                               {"package": PKG,
                                "activity": "com.zui.calendar.AllInOneActivity",
                                "sig": []}, evidence=rel)
        return ev

    def add_evidence(self, sid: int) -> int:
        return self.frame(sid)


# ───────────────────────────────────────── 清单拆条（不建表）

@case("★ 清单来自用例原文：条数与 case_expectations 一致（不新建表）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        items = sess._checklist(fx.db, sid)
        expect = common.case_expectations(fx.text)
        assert len(items) >= 3, f"176 至少有 3 条预期，实际 {len(items)}"
        assert len(items) == len(expect), (len(items), len(expect))
        assert [it["id"] for it in items] == [e["id"] for e in expect]


@case("清单初始状态全部「未勾」，finding_id 为空")
def _():
    with _Fx() as fx:
        sid = fx.session()
        items = sess._checklist(fx.db, sid)
        assert all(it["status"] is None for it in items), items
        assert all(it["finding_id"] is None for it in items), items
        assert len(sess._pending(items)) == len(items)


@case("没有用例原文的会话 → 清单为空（不抛异常）")
def _():
    with _Fx() as fx:
        sid = fx.bare_session()
        assert sess._checklist(fx.db, sid) == []


@case("勾过一条后，那条有 status/finding_id，其余仍待勾")
def _():
    with _Fx() as fx:
        sid = fx.session()
        ev = fx.add_evidence(sid)
        fid = fx.db.add_finding(sid, "PASS", "预期1 x", "看到了", "",
                                event_ids=[ev], expect_id=1, expect_label="1")
        items = sess._checklist(fx.db, sid)
        assert items[0]["status"] == "PASS", items[0]
        assert items[0]["finding_id"] == fid, items[0]
        assert len(sess._pending(items)) == len(items) - 1


# ───────────────────────────────────────── 勾选门禁（结构性）

@case("★ 没有任何事件 → 拒绝勾选，原因是「先 observe 或 act」")
def _():
    with _Fx() as fx:
        sid = fx.session()
        frame, why = sess._check_gate(fx.db, sid)
        assert frame is None
        assert "还没有任何事件" in why, why


@case("★ 有事件但都没有证据 → 拒绝勾选，原因是「带证据的事件」")
def _():
    with _Fx() as fx:
        sid = fx.session()
        fx.db.log_event(sid, "act", "tap", "点了一下", {"package": PKG,
                                                       "activity": "X", "sig": []})
        frame, why = sess._check_gate(fx.db, sid)
        assert frame is None
        assert "带证据的事件" in why, why


@case("有带证据的事件 → 放行，且返回的就是**最后那一条**")
def _():
    with _Fx() as fx:
        sid = fx.session()
        fx.frame(sid, "step01")
        last = fx.frame(sid, "step02")
        frame, why = sess._check_gate(fx.db, sid)
        assert frame is not None, why
        assert frame["id"] == last, (frame["id"], last)


@case("★ 连着勾第二次（中间没有新事件）→ 拒绝，原因写明「不能连着打勾」")
def _():
    with _Fx() as fx:
        sid = fx.session()
        ev = fx.frame(sid)
        fx.db.add_finding(sid, "PASS", "预期1 x", "看到了", "",
                          event_ids=[ev], expect_id=1, expect_label="1")
        frame, why = sess._check_gate(fx.db, sid)
        assert frame is None, frame
        assert "连着打勾" in why, why


@case("★ check 事件不算「看/动」—— 打勾本身不会把门禁打开")
def _():
    with _Fx() as fx:
        sid = fx.session()
        ev = fx.frame(sid)
        fx.db.add_finding(sid, "PASS", "预期1 x", "看到了", "",
                          event_ids=[ev], expect_id=1, expect_label="1")
        fx.db.log_event(sid, "check", "session check", "勾第1条", {"expect_id": 1})
        frame, why = sess._check_gate(fx.db, sid)
        assert frame is None, frame
        assert "连着打勾" in why, why


@case("中间补了一次 observe（有证据）→ 门禁重新放行，证据是新的那一帧")
def _():
    with _Fx() as fx:
        sid = fx.session()
        first = fx.frame(sid)
        fx.db.add_finding(sid, "PASS", "预期1 x", "看到了", "",
                          event_ids=[first], expect_id=1, expect_label="1")
        second = fx.frame(sid, "step02")
        frame, why = sess._check_gate(fx.db, sid)
        assert frame is not None, why
        assert frame["id"] == second, (frame["id"], second)


# ───────────────────────────────────────── finish 对账

@case("★ 清单没勾完就判 PASS → finish 被拦（拦在 finish_session 之前）")
def _():
    with _Fx() as fx:
        sid = fx.session()
        ev = fx.frame(sid)
        fx.db.add_finding(sid, "PASS", "预期1 x", "看到了", "",
                          event_ids=[ev], expect_id=1, expect_label="1")
        items = sess._checklist(fx.db, sid)
        pend = sess._pending(items)
        assert pend, "构造失败：应当还有没勾的"
        # 这条用例的 finish 分支会 SystemExit；这里直接复现同一判据
        assert len(pend) == len(items) - 1


@case("清单全部勾完 → 没有待勾项")
def _():
    with _Fx() as fx:
        sid = fx.session()
        items = sess._checklist(fx.db, sid)
        for it in items:
            ev = fx.frame(sid, f"step{it['id']:02d}")
            fx.db.add_finding(sid, "PASS", f"预期{it['label']} {it['text']}",
                              "看到了", "", event_ids=[ev],
                              expect_id=it["id"], expect_label=it["label"])
        left = sess._pending(sess._checklist(fx.db, sid))
        assert left == [], [it["id"] for it in left]


# ───────────────────────────────────────── CLI 层（真实进程）

@case("★ CLI：checklist 能列出清单，check 缺 --actual 也能跑通并自动绑证据")
def _():
    import json
    import os
    import subprocess

    with _Fx() as fx:
        sid = fx.session()
        fx.frame(sid)
        env = dict(os.environ)
        env["ANDROID_TEST_STORAGE"] = str(fx.root)
        env["ANDROID_TEST_RECORD"] = str(fx.root)
        # 会话要能被工具侧认出来：把当前会话指针指过去
        (fx.root / "current_session").write_text(str(sid), encoding="utf-8")

        def run(*argv):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "session.py"), *argv],
                capture_output=True, text=True, encoding="utf-8", env=env,
                cwd=str(ROOT))
            out = proc.stdout.strip()
            data = None
            if out:
                try:
                    data = json.loads(out.splitlines()[-1])
                except ValueError:
                    data = None
            return proc.returncode, data, out, proc.stderr

        rc, data, out, err = run("checklist")
        assert rc == 0, (rc, out[-800:], err[-800:])
        assert data and data.get("total", 0) >= 3, data
        assert data["pending"] == data["total"], data

        rc, data, out, err = run("check", "--status", "PASS", "--expect-id", "1",
                                 "--actual", "进入空课表，第一节无加号")
        assert rc == 0, (rc, out[-800:], err[-800:])
        assert data["ok"] is True, data
        assert data["expect_id"] == 1, data
        assert data["evidence_event_id"], data
        assert data["remaining"] >= 1, data

        # 立刻再勾第二条 → 中间没有新事件，必须被拒
        rc2, data2, out2, err2 = run("check", "--status", "PASS", "--expect-id", "2",
                                     "--actual", "又勾一条")
        assert rc2 != 0, (rc2, out2[-800:])
        assert "连着打勾" in (out2 + err2), (out2[-800:], err2[-800:])

        # 重复勾第一条 → 拒绝
        rc3, data3, out3, err3 = run("check", "--status", "PASS", "--expect-id", "1",
                                     "--actual", "重复")
        assert rc3 != 0, (rc3, out3[-500:])


def main() -> int:
    failed = []
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            failed.append(name)
            print(f"FAIL {name}\n     {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL {name}\n     {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print()
    if failed:
        print(f"❌ {len(failed)}/{len(CASES)} 条失败：{'、'.join(failed)}")
        return 1
    print(f"✅ 全部通过（{len(CASES)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
