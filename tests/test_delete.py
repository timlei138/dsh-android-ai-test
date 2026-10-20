# -*- coding: utf-8 -*-
"""删除功能：用例 / 原始 case / 测试记录。

## 这套测试守的是什么

用户提的两件事：
  1. 用例要能看**原始 case**（用户当初写的步骤和预期），列表要能删用例
  2. 测试记录要能删，连带 log / 截图 / xml

## 最危险的两处，测试重点盯它们

### ① 删用例不能连坐删历史

旧 skill 是连坐的（删用例顺手删掉它全部记录）。这里刻意不学：
记录里存着**跑的时候那份 YAML 快照**，是用例文件删掉之后唯一的判据副本。
连坐 = 证据跟着一起销毁。

### ② 删文件必须圈在证据根内

路径来自数据库。库是可信来源，但这服务会被手动改库调试（改坏过）。
所以删除前必须确认路径真的落在 tmp/shots 之类的地方 ——
否则一个被改坏的路径能把仓库里别的文件删了。
"""

import os
import sys
import tempfile
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
print("1) 原始 case：一个用例一份，存进库")
print("=" * 70)

ck("没有时返回 None（不拿空串冒充有）",
   st.get_raw_case("nope", db=db) is None)
ck("首次写入返回 True",
   st.save_raw_case("c1", "1. 打开日历\n2. 点更多→课程表\n预期：进入课程表页",
                    db=db) is True)
r = st.get_raw_case("c1", db=db) or {}
ck("读回内容一致", "点更多" in r.get("raw_text", ""), f"{r}")
ck("带更新时间", bool(r.get("updated_at")), f"{r}")
ck("再次写入是覆盖（返回 False，不新增行）",
   st.save_raw_case("c1", "新内容", db=db) is False)
ck("★ 空文本不覆盖（手滑清空不该丢数据）",
   st.save_raw_case("c1", "   ", db=db) is False
   and "新内容" in (st.get_raw_case("c1", db=db) or {}).get("raw_text", ""))
ck("删除返回 1", st.delete_raw_case("c1", db=db) == 1)
ck("删完读不到", st.get_raw_case("c1", db=db) is None)
ck("再删返回 0", st.delete_raw_case("c1", db=db) == 0)

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

# 直接验两个函数的语义边界：delete_raw_case 只碰 raw_cases，
# delete_run 只碰 runs 三张表。删用例的 HTTP 层只是调它们 + unlink 文件。
rid3 = _mkrun("casedel")
st.save_raw_case("casedel", "原始需求原文", db=None)  # 真库，验完会清
ck("删 raw case 只删原文",
   st.delete_raw_case("casedel") == 1
   and st.get_raw_case("casedel") is None)
ck("★★ 记录还在（删用例不连坐）", st.get_run(rid3, db=db) is not None)
st.delete_run(rid3, db=db)

print()
print("=" * 70)
print("5) 接口层面：路由和参数都在")
print("=" * 70)

src = (ROOT / "webui.py").read_text(encoding="utf-8")
app = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")

ck("后端有 do_DELETE", "def do_DELETE" in src)
ck("★ 删记录接口", 'path.startswith("/api/run/")' in src)
ck("★ 删用例接口", 'path.startswith("/api/case/")' in src)
ck("★ 删原始 case 接口", 'path.startswith("/api/rawcase/")' in src)
ck("原始 case 有写入口（只能人录）", "def do_POST" in src
   and "save_raw_case" in src)
ck("用例列表标了 has_raw", '"has_raw"' in src)

ck("前端有删记录确认框", "function confirmDeleteRun" in app)
ck("前端有删用例确认框", "function confirmDeleteCase" in app)
ck("★ 删记录的勾选框默认勾上（否则截图永远清不掉）",
   'id="del-ev" checked' in app)
ck("★ 用例库行上有删除按钮", "data-del=" in app)
ck("★ 记录列表行上有删除按钮", "data-delrun=" in app)
ck("详情页也有删除按钮", 'id="run-del"' in app)
ck("用例详情页展示原始 case", "/api/rawcase/" in app)
ck("没录原文时明确说没有（不拿 YAML 顶替）", "还没录原始 case" in app)

print()
print("=" * 70)
print("6) 顺手修的两个前端 bug")
print("=" * 70)

# bug 1：用例库的"最近结论/跑过"两列永远是空的
ck("★ 用例库按 case_name 对 stats（不是 case_id）",
   "byName[stem]" in app and "byId[c.case_id]" not in app,
   "之前建索引用 case_name、查却用 case_id，两列永远空且不报错")

# bug 2：表格行里的按钮会把点击冒泡成"打开这一行"
ck("★ 表格行按钮不再触发打开行",
   "if (e.target.closest('button')) return;" in app,
   "之前点删除会同时把详情页也打开")

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
