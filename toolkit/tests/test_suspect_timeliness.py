"""suspect_timeliness：PASS 判定的「时效自相矛盾」自检。

为什么需要这个自检
------------------
另一台设备上 176（新建课程表）唯一一次 PASS 是假的：预期3 写「进入空课程表后
第一个小节显示加号」，那一轮却拿**点过格子之后**拍的帧去验。实际产品行为是
「进入时没有、点中格子才浮出加号」—— 11 轮实测一致。本机也有两处同款：
#619（session 152）、#1169（session 1000066），都是 actual 里自己写着
「进入时无 X … 点击后新出现 X」，却判了 PASS。

这个自检只做一件事：**从 finding 自己的 actual 文本里认出这种自相矛盾，
在 note 上挂一句提醒**。它不改 status —— 文本启发式宁可漏不可错，
判错了把真 PASS 标成可疑，比漏报一条更糟。

判据刻意极窄：必须「同一样东西」在 actual 里先被否定、后被说成"操作之后才出现"，
且该 token 在文本里出现 ≥2 次。在真库 1548 条 PASS 上命中 2 条，都是真问题。
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import db as dbm  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def suspect(expect, actual):
    return dbm.SessionDB.suspect_timeliness(expect, actual)


# 真实样本（来自真库 #1169 的 actual，逐字）
REAL_1169 = ("空周视图初进入时 20 个格子只有 rid=cv_empty_content、无 iv_add_hint。"
             "点击第一个小节格子后 dump 里第一格立即出现 rid=iv_add_hint "
             "bounds 486,582-618,714")

# 真实样本（来自真库 #619 的 actual 摘要）
REAL_619 = ("进入空表时第一小节格子内无加号。点击第一小节后该格内新出现 "
            "rid=iv_add_hint。注意：加号是点击格子后才显示，非进入页面即显示")


@case("★ 真库 #1169 原文命中")
def _():
    msg = suspect("预期3（后半）：进入空课程表后第一个小节显示加号示意", REAL_1169)
    assert msg, "真库 #1169 必须命中"
    assert "iv_add_hint" in msg
    assert "【时效待复核】" in msg


@case("★ 真库 #619 原文命中")
def _():
    msg = suspect("预期3b 空课表第一小节显示加号示意可点击添加课程", REAL_619)
    assert msg, "真库 #619 必须命中"
    assert "加号" in msg


@case("空 actual → 不命中")
def _():
    assert suspect("预期3", "") == ""
    assert suspect("预期3", None) == ""


@case("只有否定、没有「操作后才出现」→ 不命中")
def _():
    assert suspect("x", "进入页面时弹框无取消按钮") == ""


@case("只有「点击后出现」、没有否定 → 不命中")
def _():
    assert suspect("x", "点击右上角按钮后出现弹框") == ""


@case("否定与出现说的不是同一样东西 → 不命中")
def _():
    # 否掉的是"取消按钮"，后来出现的是"弹框" —— 不是同一样东西，不算自相矛盾
    txt = "进入页面时无取消按钮；点击右上角后出现弹框"
    assert suspect("x", txt) == ""


@case("同一样东西但只出现一次 → 不命中（要求 >=2 次）")
def _():
    txt = "无加号；点击后出现别的提示语"
    assert suspect("x", txt) == ""


@case("正常「默认值 + 再操作」描述 → 不命中")
def _():
    assert suspect("x", "默认显示今天的日期，点击下一天后日期变为明天") == ""
    assert suspect("x", "第一格有加号，点击后进入新建课程页") == ""
    assert suspect("x", "通知栏有 3 条通知，点击后展开列表") == ""


@case("★ FAIL/BLOCKED 状态不该被这个判据影响（调用方只在 PASS 时调）")
def _():
    # 方法是纯函数，不论传什么它都会算；这里断言的是「文档化契约」：
    # 调用方 add_finding 只在 status == 'PASS' 时调用。见下面 _add_finding_note 用例。
    assert suspect("预期3", REAL_1169) != ""


def _mk_db(tmp):
    d = dbm.SessionDB(tmp / "t.db")
    sid = d.start_session("suspect test", "t")
    ev = tmp / "shot.png"
    ev.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    eid = d.log_event(sid, "act", "tap", "test", evidence=str(ev))
    return d, sid, eid


def _note(d, fid):
    return d._conn().execute("SELECT note FROM findings WHERE id=?", (fid,)).fetchone()[0]


@case("★ add_finding：PASS + 时效矛盾 → note 自动带【时效待复核】")
def _():
    import tempfile
    tmp = Path(tempfile.mkdtemp(dir=str(ROOT / "storage" / "tmp")))
    d, sid, eid = _mk_db(tmp)
    try:
        fid = d.add_finding(sid, "PASS", "预期3：进入后第一个小节显示加号",
                            REAL_1169, event_ids=[eid])
        note = _note(d, fid)
        assert "【时效待复核】" in note, note
    finally:
        c = getattr(d._local, "conn", None)
        if c:
            c.close()
            d._local.conn = None


@case("★ add_finding：正常 PASS 不带该标记")
def _():
    import tempfile
    tmp = Path(tempfile.mkdtemp(dir=str(ROOT / "storage" / "tmp")))
    d, sid, eid = _mk_db(tmp)
    try:
        fid = d.add_finding(sid, "PASS", "预期1：点击进入下一页",
                            "点击后进入下一页，标题正确", event_ids=[eid])
        assert "【时效待复核】" not in _note(d, fid)
    finally:
        c = getattr(d._local, "conn", None)
        if c:
            c.close()
            d._local.conn = None


@case("★ add_finding：FAIL 即使文本矛盾也不加该标记")
def _():
    import tempfile
    tmp = Path(tempfile.mkdtemp(dir=str(ROOT / "storage" / "tmp")))
    d, sid, eid = _mk_db(tmp)
    try:
        fid = d.add_finding(sid, "FAIL", "预期3：进入后显示加号", REAL_1169,
                            event_ids=[eid])
        assert "【时效待复核】" not in _note(d, fid)
    finally:
        c = getattr(d._local, "conn", None)
        if c:
            c.close()
            d._local.conn = None


@case("★ add_finding：人写的 note 不被覆盖，只追加")
def _():
    import tempfile
    tmp = Path(tempfile.mkdtemp(dir=str(ROOT / "storage" / "tmp")))
    d, sid, eid = _mk_db(tmp)
    try:
        fid = d.add_finding(sid, "PASS", "预期3", REAL_1169,
                            note="我自己写的备注", event_ids=[eid])
        note = _note(d, fid)
        assert note.startswith("我自己写的备注"), note
        assert "【时效待复核】" in note
    finally:
        c = getattr(d._local, "conn", None)
        if c:
            c.close()
            d._local.conn = None


@case("★ 真库回归：全量 PASS 命中数必须很小（<=5），且两条已知样本都在")
def _():
    import sqlite3
    db = ROOT / "storage" / "test.db"
    if not db.is_file():
        print("    (skip: 无真库)")
        return
    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    try:
        rows = con.execute(
            "SELECT id, actual FROM findings WHERE status='PASS'").fetchall()
    finally:
        con.close()
    if not rows:
        print("    (skip: 库里没有 PASS)")
        return
    hits = {fid for fid, actual in rows if suspect("", actual)}
    assert len(hits) <= 5, "命中 %d 条，判据过宽会大面积误报: %s" % (
        len(hits), sorted(hits)[:20])
    # 真库里的两个已确认样本必须命中
    assert 619 in hits, "真库 #619 是已知正样本，必须命中"
    assert 1169 in hits, "真库 #1169 是已知正样本，必须命中"


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    ok = bad = 0
    for name, fn in CASES:
        try:
            fn()
            ok += 1
            print("PASS  " + name)
        except AssertionError as e:
            bad += 1
            print("FAIL  %s\n      %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            bad += 1
            print("ERROR %s\n      %s: %s" % (name, type(e).__name__, e))
    print()
    print("%d/%d 通过" % (ok, ok + bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
