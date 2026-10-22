# -*- coding: utf-8 -*-
"""删除功能：用例 / 原始 case / 测试记录。

## 这套测试守的是什么

用户提的两件事：
  1. 用例要能看**原始 case**（用户当初写的步骤和预期），列表要能删用例
  2. 测试记录要能删，连带 log / 截图 / xml

## 最危险的两处，测试重点盯它们

### ① 删用例不能连坐删历史

旧 skill 是连坐的（删用例顺手删掉它全部记录）。这里刻意不学：
用例删掉之后历史记录仍然可复核（证据在 tmp/ 下，跟用例无关）。
连坐 = 证据跟着一起销毁。

### ② 删文件必须圈在证据根内

路径来自数据库。库是可信来源，但这服务会被手动改库调试（改坏过）。
所以删除前必须确认路径真的落在 tmp/shots 之类的地方 ——
否则一个被改坏的路径能把仓库里别的文件删了。
"""

import os
import sys
import tempfile
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANDROID_SKILL_ROOT", str(ROOT / "assets"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.reporting.store as st  # noqa: E402
import webui  # noqa: E402

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


db = Path(tempfile.mkdtemp()) / "t.db"


def _mkrun(name, shots=(), log=None):
    return st.save_run(
        case_name=name,
        report={"case_id": "1", "status": "PASS", "summary": "s",
                "steps_done": 1, "elapsed_s": 1.0, "progress": [{"t": 1.0}],
                "findings": [{"kind": "assert", "id": "a1", "desc": "d",
                              "status": "PASS", "actual": "x", "issues": []}],
                "strong_stats": {"auto_rate": 1.0}},
        ledger_rows=[{"seq": i + 1, "step": "s0", "action": "click",
                      "x": 1, "y": 2, "before_act": "A", "after_act": "B",
                      "changed": 1, "ms": 5,
                      **({"shot_before": str(p)} if p else {})}
                     for i, p in enumerate(shots)],
        paths={"log": log} if log else None,
        db=db)


print("=" * 70)
print("1) 用例：原文存取")
print("=" * 70)

ck("没有时返回 None（不拿空串冒充有）", st.get_case("999", db=db) is None)
ck("首次写入是 created",
   st.save_case("999", title="测试用例", priority="P1",
                raw_text="1. 打开日历\n2. 点更多→课程表\n预期：进入课程表页",
                source_path="D:/x/999_日历_999.txt", db=db) == "created")
r = st.get_case("999", db=db) or {}
ck("读回内容一致", "点更多" in r.get("raw_text", ""), f"{r}")
ck("标题和优先级都存住了",
   r.get("title") == "测试用例" and r.get("priority") == "P1", f"{r}")
ck("来源路径留痕", "999_日历_999.txt" in (r.get("source_path") or ""), f"{r}")
ck("带更新时间", bool(r.get("updated_at")), f"{r}")

ck("再写是 updated（不新增行）",
   st.save_case("999", raw_text="新内容", db=db) == "updated")
ck("★ 空文本不覆盖（手滑清空不该丢数据）",
   st.save_case("999", raw_text="   ", db=db) == "skipped"
   and "新内容" in (st.get_case("999", db=db) or {}).get("raw_text", ""))

ck("空文本改原文返回 False", st.save_case_raw("999", "  ", db=db) is False)
ck("删除返回 1", st.delete_case("999", db=db) == 1)
ck("删完读不到", st.get_case("999", db=db) is None)
ck("再删返回 0", st.delete_case("999", db=db) == 0)

print()
print("=" * 70)
print("1b) 编号解析：取最后一个数字段")
print("=" * 70)
ck("★★ '13_日历_167.txt' 取 167 而不是 13（排序前缀不能当编号）",
   st.case_number("13_日历_167.txt") == "167", st.case_number("13_日历_167.txt"))
ck("'03_日历_03.txt' -> 03", st.case_number("03_日历_03.txt") == "03")
ck("用例文件名取编号",
   st.case_number("联想日历_167_首次进入课程表引导与空态") == "167",
   st.case_number("联想日历_167_首次进入课程表引导与空态"))
ck("纯数字名也能解析", st.case_number("167") == "167")
ck("没有数字返回空串（不瞎猜）", st.case_number("nope") == "")

print()
print("=" * 70)
print("2) 删记录：三张表都清干净，别的记录不受牵连")
print("=" * 70)

rid = _mkrun("delme", shots=["tmp/shots/a.png"])
ck("先有一条", st.get_run(rid, db=db) is not None)
res = st.delete_run(rid, db=db)
ck("删掉 1 条", res.get("deleted") == 1, f"{res}")
ck("记录没了", st.get_run(rid, db=db) is None)
ck("带回 case_name（前端要知道刷新哪）",
   res.get("case_name") == "delme", f"{res.get('case_name')}")
ck("★ 收齐了证据路径（交给上层清磁盘）",
   any("a.png" in p for p in (res.get("paths") or [])), f"{res.get('paths')}")

con = st.connect(db)
n_a = con.execute("SELECT COUNT(*) FROM actions WHERE run_id=?",
                  (rid,)).fetchone()[0]
n_f = con.execute("SELECT COUNT(*) FROM findings WHERE run_id=?",
                  (rid,)).fetchone()[0]
con.close()
ck("★ actions 级联删干净", n_a == 0, f"还剩 {n_a}")
ck("★ findings 级联删干净", n_f == 0, f"还剩 {n_f}")

rid2 = _mkrun("keepme")
st.delete_run(rid, db=db)
ck("★ 删 A 不影响 B", st.get_run(rid2, db=db) is not None)
ck("删不存在的返回 deleted=0",
   st.delete_run(999999, db=db).get("deleted") == 0)

print()
print("=" * 70)
print("3) 删文件的边界：只删证据根里面的")
print("=" * 70)

sandbox = Path(tempfile.mkdtemp())
evroot = sandbox / "shots"
evroot.mkdir()
step = evroot / "step01"
step.mkdir()
good_png = step / "shot.png"
good_png.write_bytes(b"x")
good_xml = step / "dump.xml"
good_xml.write_text("<x/>", encoding="utf-8")

# 证据根**外面**的文件，绝不能被删
outside = sandbox / "important.txt"
outside.write_text("别删我", encoding="utf-8")
# 兄弟目录：/sandbox/shots 的前缀相同但不是它内部
sibling = sandbox / "shots2"
sibling.mkdir()
sib_file = sibling / "keep.png"
sib_file.write_bytes(b"y")

_orig_roots = webui._evidence_roots
webui._evidence_roots = lambda: [evroot]
try:
    res = webui._purge_evidence([str(good_png), str(good_xml),
                                 str(outside), str(sib_file)])
    ck("证据根内的文件删掉了", not good_png.exists() and not good_xml.exists())
    ck("删完清了 2 个文件", res["files"] == 2, f"{res}")
    ck("★★ 证据根外的文件没动", outside.exists())
    ck("★★ 前缀相同的兄弟目录没动（shots2 ≠ shots 内部）", sib_file.exists())
    ck("跳过的记下来了", len(res["skipped"]) == 2, f"{res['skipped']}")
    ck("★ 空的 step 目录顺手清掉", not step.exists())
    ck("★ 证据根本身不被删", evroot.exists())

    # 路径穿越
    res2 = webui._purge_evidence(["../../etc/passwd"])
    ck("穿越路径被跳过", res2["files"] == 0 and res2["skipped"],
       f"{res2}")
finally:
    webui._evidence_roots = _orig_roots

print()
print("=" * 70)
print("4) ★★ 删用例不连坐历史（这条最容易做错）")
print("=" * 70)

# 直接验语义边界：delete_case 只碰 cases 表，delete_run 只碰 runs 三张表。
# 删用例的 HTTP 层只是调它们 + unlink 文件。
rid3 = _mkrun("casedel")
st.save_case("555", raw_text="原始需求原文", db=db)
ck("删用例只删那一行",
   st.delete_case("555", db=db) == 1 and st.get_case("555", db=db) is None)
ck("★★ 记录还在（删用例不连坐）", st.get_run(rid3, db=db) is not None)
st.delete_run(rid3, db=db)

print()
print("=" * 70)
print("6) 共享文件：同用例的日志是共用的，删一条不能毁掉别人的证据")
print("=" * 70)

# 这是实测发现的真 bug：run_167.log / clicks_167.jsonl 按用例名命名、
# 每次运行追加，7 条 167 记录全指向同几个文件。第一版 delete_run 无脑
# 把这些路径都交出去 unlink —— 删任意一条，另外 6 条的日志和点击账本
# 就一起没了，而它们看起来还好好的，只是点开是空的。
shared_log = "tmp/run_sharedcase.log"
shared_ledger = "tmp/clicks_sharedcase.jsonl"
a = _mkrun("sharedcase", log=shared_log)
b = _mkrun("sharedcase", log=shared_log)
# 手工把 ledger 也指成同一个
con = st.connect(db)
con.execute("UPDATE runs SET ledger_path = ? WHERE run_id IN (?,?)",
            (shared_ledger, a, b))
con.commit()
con.close()

res_a = st.delete_run(a, db=db)
ck("删掉 a", res_a.get("deleted") == 1, f"{res_a}")
ck("★★ 共用的 log 没进待删列表",
   shared_log not in (res_a.get("paths") or []), f"{res_a.get('paths')}")
ck("★★ 共用的 ledger 没进待删列表",
   shared_ledger not in (res_a.get("paths") or []), f"{res_a.get('paths')}")
ck("★ 共享文件被单独报出来（好让前端告诉用户保留了什么）",
   len(res_a.get("shared") or []) == 2, f"{res_a.get('shared')}")
ck("★ b 的记录还在", st.get_run(b, db=db) is not None)

# 删掉 b 之后，这些文件就没人引用了 -> 可以删
res_b = st.delete_run(b, db=db)
ck("★ 最后一个持有者删掉后，文件转为可删",
   shared_log in (res_b.get("paths") or []), f"{res_b.get('paths')}")
ck("此时 shared 为空", not (res_b.get("shared") or []), f"{res_b.get('shared')}")

# 截图按 step 目录分，通常不共享，但同样要按引用数判断。
#
# ⚠️ 两个坑，都踩过：
#   1. 用不存在的假路径测 —— delete_run 对它走"文件不在了"分支，
#      删不删都看不出来，测了个寂寞。必须用真实文件。
#   2. 真实文件的 mtime 是"刚才"，而 started_at 是"落库时刻 - elapsed"，
#      于是它天然被判成"比本次运行新"= 过期。所以要把 mtime 拨回到
#      运行开始之前，才是"这次运行写下的文件"该有的样子。
indep_f = Path(tempfile.mkdtemp()) / "shot.png"
indep_f.write_bytes(b"i")
_started = _time.time() - 300
os.utime(indep_f, (_started, _started))   # 假装是 5 分钟前那次运行写的
indep = str(indep_f)
c = _mkrun("indep", shots=[indep])
res_c = st.delete_run(c, db=db)
ck("独享的截图照常可删", indep in (res_c.get("paths") or []),
   f"{res_c.get('paths')} / stale={res_c.get('stale')}")

print()
print("=" * 70)
print("7) 过期文件：被后续运行覆盖过的截图不属于这次运行")
print("=" * 70)

# tmp/shots/stepNN/ 是按步号复用的，不是按 run 隔离。下一次运行会覆盖
# 上一次的 shot.png。所以库里 #10 和 #15 都记着同一个 step04/shot.png，
# 而磁盘上那份已经是后来那次的了。引用计数判不出来（后来的记录可能已删），
# 只能看时间戳：文件比本次运行开始还新，就不是这次的证据。
#
# 用真文件 + 真时间戳验，不用 mock —— 这个判断整个建立在时间比较上。

tmpdir = Path(tempfile.mkdtemp())
old_f = tmpdir / "old.png"
new_f = tmpdir / "new.png"
old_f.write_bytes(b"o")
new_f.write_bytes(b"n")
now = _time.time()
os.utime(old_f, (now - 3600, now - 3600))   # 一小时前
os.utime(new_f, (now + 60, now + 60))       # 还没到（模拟比 started_at 新）

con = st.connect(db)
con.execute("DELETE FROM actions")
con.execute("DELETE FROM runs")
con.commit()
con.close()

# ⚠️ 时间判断只对**老布局**（stepNN/，按步号复用）生效。
#    新布局 run_xxx/ 是按运行隔离的，天生属于这次运行，不该被时间否决。
tmpdir = Path(tempfile.mkdtemp())
old_f = tmpdir / "old.png"
new_f = tmpdir / "new.png"
old_f.write_bytes(b"o")
new_f.write_bytes(b"n")
now = _time.time()
os.utime(old_f, (now - 3600, now - 3600))   # 一小时前
os.utime(new_f, (now + 600, now + 600))     # 十分钟后（比运行结束还新）

rid_ts = st.save_run(
    case_name="tscase",
    report={"case_id": "1", "status": "PASS", "summary": "s", "steps_done": 1,
            "elapsed_s": 1.0, "progress": [{"t": now - 3600}], "findings": [],
            "strong_stats": {"auto_rate": 1.0}},
    ledger_rows=[{"seq": 1, "step": "s0", "action": "click", "x": 1, "y": 2,
                  "before_act": "A", "after_act": "B", "changed": 1, "ms": 5,
                  "shot_before": str(old_f), "shot_after": str(new_f)}],
    db=db)

res_ts = st.delete_run(rid_ts, db=db)
ck("旧文件判为本次的，可删",
   str(old_f) in (res_ts.get("paths") or []), f"{res_ts.get('paths')}")
ck("★★ 比运行结束还新的老布局文件判为过期（被后来的运行覆盖了）",
   str(new_f) not in (res_ts.get("paths") or []), f"{res_ts.get('paths')}")
ck("★ 过期文件单独报出来",
   len(res_ts.get("stale") or []) == 1, f"{res_ts.get('stale')}")

# ★★ 新布局的文件即使比运行结束新，也必须能删 —— 它天生属于这次运行。
#    这条是回归：第一版没区分布局，把新布局的文件也判成过期，
#    结果"删记录"一个文件都不清，功能形同虚设。
rundir = Path(tempfile.mkdtemp()) / "shots" / "run_20260101_000000" / "step03"
rundir.mkdir(parents=True)
newlayout_f = rundir / "shot.png"
newlayout_f.write_bytes(b"n")
os.utime(newlayout_f, (now + 600, now + 600))   # 同样比运行结束新
rid_nl = st.save_run(
    case_name="newlayout",
    report={"case_id": "1", "status": "PASS", "summary": "s", "steps_done": 1,
            "elapsed_s": 1.0, "progress": [{"t": now - 3600}], "findings": [],
            "strong_stats": {"auto_rate": 1.0}},
    ledger_rows=[{"seq": 1, "step": "s0", "action": "click", "x": 1, "y": 2,
                  "before_act": "A", "after_act": "B", "changed": 1, "ms": 5,
                  "shot_before": str(newlayout_f)}],
    db=db)
res_nl = st.delete_run(rid_nl, db=db)
ck("★★ 新布局的文件不受时间判断影响（run_xxx/ 天生属于这次运行）",
   str(newlayout_f) in (res_nl.get("paths") or []),
   f"paths={res_nl.get('paths')} stale={res_nl.get('stale')}")

# started_at 解析不出来时不启用时间判断（否则老记录永远删不掉）
con = st.connect(db)
rid_nots = st.save_run(
    case_name="nots",
    report={"case_id": "1", "status": "PASS", "summary": "s", "steps_done": 1,
            "elapsed_s": 1.0, "progress": [{"t": 1.0}], "findings": [],
            "strong_stats": {"auto_rate": 1.0}},
    ledger_rows=[{"seq": 1, "step": "s0", "action": "click", "x": 1, "y": 2,
                  "before_act": "A", "after_act": "B", "changed": 1, "ms": 5,
                  "shot_before": str(new_f)}],
    db=db)
con = st.connect(db)
con.execute("UPDATE runs SET started_at = NULL WHERE run_id = ?", (rid_nots,))
con.commit()
con.close()
res_nots = st.delete_run(rid_nots, db=db)
ck("取不到开始时间时不启用时间判断（否则老记录删不掉）",
   str(new_f) in (res_nots.get("paths") or []), f"{res_nots.get('paths')}")

print()
print("=" * 70)
print("8) started_at 不能是 1970（这条会让统计和删记录全错）")
print("=" * 70)

# 背景：_tick 写的是 time.time()（epoch 秒），所以 progress[0].t 可以是
# started_at。但如果传进来的是个**小数字**（相对耗时 1.0、或者手搓的
# 测试数据），直接当 epoch 用就会渲染成 1970-01-01 —— 而且不报错，
# 只是"最近三天"统计空了、对比页时间戳荒唐、删记录时把文件全判成过期。
# 现在加了 1e9 这道门槛：明显不可能是时间戳的值一律不认。
cases = [
    ("真时间戳（epoch 秒）直接用", _time.time() - 100, True),
    ("相对耗时 1.0 不认", 1.0, False),
    ("0.0 不认", 0.0, False),
    ("None 不认", None, False),
]
for label, t_val, should_be_recent in cases:
    probe_db = Path(tempfile.mkdtemp()) / "ts.db"
    rid_probe = st.save_run(
        case_name="p",
        report={"case_id": "1", "status": "PASS", "summary": "s",
                "steps_done": 1, "elapsed_s": 10.0,
                "progress": [{"t": t_val}], "findings": [],
                "strong_stats": {"auto_rate": 1.0}},
        ledger_rows=[], db=probe_db)
    sa = (st.get_run(rid_probe, db=probe_db) or {}).get("started_at") or ""
    year = sa[:4]
    if should_be_recent:
        ck(label, year >= "2020", f"started_at={sa!r}")
    else:
        ck(label, year >= "2020", f"started_at={sa!r}（不该是 1970）")

print()
print("=" * 70)
print("9) 按运行隔离的证据目录：整个丢，连记录没引用的中间文件也清掉")
print("=" * 70)

# 一次运行除了 shot.xml 还会写 meta.json / ocrNN 之类，
# 这些不在 actions 表里。只删"记录引用过的"会让目录剩一堆孤儿文件。
sandbox2 = Path(tempfile.mkdtemp())
ev2 = sandbox2 / "shots"
rundir = ev2 / "run_20261020_120000"
(rundir / "step03").mkdir(parents=True)
(rundir / "step04").mkdir(parents=True)
shot = rundir / "step03" / "shot.png"
shot.write_bytes(b"s")
xmlf = rundir / "step03" / "dump.xml"
xmlf.write_text("<x/>", encoding="utf-8")
meta = rundir / "step03" / "meta.json"          # 记录里没有它
ocr = rundir / "step04" / "ocr01.png"           # 记录里也没有它

# 老布局（共用 stepNN）—— 绝不能整目录删
legacy = ev2 / "step05"
legacy.mkdir()
legacy_f = legacy / "shot.png"
legacy_f.write_bytes(b"L")

_orig2 = webui._evidence_roots
webui._evidence_roots = lambda: [ev2]
try:
    res = webui._purge_evidence([str(shot), str(xmlf), str(legacy_f)])
    ck("★ 按运行隔离的整个目录被删掉", not rundir.exists())
    ck("★★ 记录没引用的中间文件（meta.json/ocr）也一并清掉",
       not meta.exists() and not ocr.exists())
    ck("目录名记在 dirs 里", "run_20261020_120000" in (res.get("dirs") or []),
       f"{res.get('dirs')}")
    # ⚠️ 老布局的 stepNN 目录：文件删掉后它变空，会被 rmdir。
    #    这是**要的**（不留空壳），但和"整目录 rmtree"有本质区别 ——
    #    如果目录里还有别人的文件，它就不会变空、也就不会被删。
    ck("★ 老布局只删自己的文件（目录内没别人的东西就成空壳被收走）",
       not legacy_f.exists(), f"legacy_f 还在: {legacy_f.exists()}")
    ck("★ 证据根本身不动", ev2.exists())

    # ★ 关键一条：老布局目录里**还有别人的文件**时，绝不能连坐删掉
    d3 = ev2 / "step05"
    d3.mkdir(exist_ok=True)
    mine = d3 / "mine.png"
    mine.write_bytes(b"m")
    other = d3 / "other.png"      # 别的记录的文件，不在待删列表里
    other.write_bytes(b"o")
    webui._purge_evidence([str(mine)])
    ck("★★ 只删自己那个，别人同目录的文件不动",
       not mine.exists() and other.exists())
    ck("★★ 目录里还有别人的文件，目录本身保留", d3.exists())

    # 目录名不以 run_ 开头的一律不整删，哪怕它在证据根内
    d2 = ev2 / "shared_step99"
    d2.mkdir(exist_ok=True)
    f2 = d2 / "shot.png"
    f2.write_bytes(b"z")
    keep2 = d2 / "meta.json"
    keep2.write_bytes(b"k")
    webui._purge_evidence([str(f2)])
    ck("★★ 不是 run_ 前缀的目录不会整删（目录里未引用的文件保住）",
       not f2.exists() and keep2.exists(), f"keep2 还在: {keep2.exists()}")
finally:
    webui._evidence_roots = _orig2

print()
print("=" * 70)
print("10) 接口层面：路由和参数都在")
print("=" * 70)

src = (ROOT / "webui.py").read_text(encoding="utf-8")
app = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")

ck("后端有 do_DELETE", "def do_DELETE" in src)
ck("★ 删记录接口", 'path.startswith("/api/run/")' in src)
ck("★★ 删用例接口",
   'path.startswith("/api/cases/")' in src and "st.delete_case(key)" in src)
ck("改原文接口", 'path.endswith("/raw")' in src and "save_case_raw" in src)
ck("用例列表带跑过几次", '"last_status"' in src)
# ⚠️ runs.case_name 存的是编号，不是文件名。拿文件名去对必然空，且不报错。
ck("★★ 统计按编号对（runs.case_name 存的是编号）",
   '_run_stats_by_case' in src and 'by_case.get(str(c["case_key"])' in src,
   "拿文件名去对会整列显示未跑过")

ck("前端有删记录确认框", "function confirmDeleteRun" in app)
ck("前端有删用例确认框", "async function confirmDeleteCase" in app)
ck("★ 删记录的勾选框默认勾上（否则截图永远清不掉）",
   'id="del-ev" checked' in app)
ck("★ 用例库行上有删除按钮", "data-delcase=" in app)
ck("★ 记录列表行上有删除按钮", "data-delrun=" in app)
ck("详情页也有删除按钮", 'id="run-del"' in app)
ck("用例详情页展示原始 case", "d.raw_text" in app)

print()
print("=" * 70)
print("11) 顺手修的两个前端 bug")
print("=" * 70)

# bug 1：用例库的"最近结论/跑过"两列永远是空的
#   第一次是"建索引一个键、查另一个键"；现在改成后端算好、前端直接用，
#   前端不再自己建索引，这一类错就没地方藏了。
ck("★ 用例库直接用后端算好的 runs/last_status（前端不再自建索引）",
   "c.runs" in app and "c.last_status" in app and "byId[c.case_id]" not in app,
   "之前建索引用一种键、查用另一种，两列永远空且不报错")

# bug 2：表格行里的按钮会把点击冒泡成"打开这一行"
ck("★ 表格行按钮不再触发打开行",
   "if (e.target.closest('button')) return;" in app,
   "之前点删除会同时把详情页也打开")

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
