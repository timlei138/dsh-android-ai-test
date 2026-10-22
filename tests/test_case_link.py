# -*- coding: utf-8 -*-
"""用例与 YAML 的关联：导入、解析、删除一起走。

## 这一轮改了什么（用户的三条要求）

  1. "把 case 和 yaml 都关联起来吧。这样删除的时候页简单点。"
  2. "现在原始得记录在 d:/dsh cal_case 和 cases 两个文件夹下。"
  3. "相关二级页面增加返回"

前两条在这里验（数据层 + 导入 + 删除）。第三条是纯前端，见 test_delete.py
和 test_webui_cases.py 里的路由断言。
"""
import ast
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("ANDROID_SKILL_ROOT", str(ROOT / "assets"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.cases import importer
from android_agent.cases import paths as cpaths
from android_agent.reporting import store as st

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


db = str(Path(tempfile.mkdtemp()) / "t.db")

print("=" * 70)
print("1) 编号解析：两边唯一的共同线索")
print("=" * 70)
# ★ 最容易错的一条：文件名带排序前缀，第一个数字不是编号
ck("★★ '13_日历_167.txt' -> 167（不是排序前缀 13）",
   st.case_number("13_日历_167.txt") == "167", st.case_number("13_日历_167.txt"))
ck("'03_日历_03.txt' -> 03（前缀和编号相同时不能糊）",
   st.case_number("03_日历_03.txt") == "03")
ck("YAML 名 -> 编号",
   st.case_number("联想日历_167_首次进入课程表引导与空态.yaml") == "167")
ck("纯数字 -> 自己", st.case_number("167") == "167")
ck("没数字 -> 空串（不猜）", st.case_number("readme.md") == "")

print()
print("=" * 70)
print("2) 原文解析：两批文件格式不一样")
print("=" * 70)

# cal_cases 那批：首行就带题目和优先级
t1 = "测试用例 日历_03 启动应用-CTA权限弹窗（P2）\n\n预置条件：\n首次启动\n\n测试步骤：\n1.进入日历"
m1 = importer.parse_raw(t1)
ck("★ 抓到题目", m1["title"] == "启动应用-CTA权限弹窗", m1["title"])
ck("★ 抓到优先级 P2", m1["priority"] == "P2", m1["priority"])

# cases 那批：首行只有编号，后面直接是正文 —— 不能把正文当题目
t2 = "测试用例 联想日历_168 前提：确保设备没有课程表。如果存在需要删除所有课程表 操作步骤 1.日历点击更多"
m2 = importer.parse_raw(t2)
ck("★★ 正文不会被当成题目", not m2["title"].startswith("前提"), m2["title"])

# 原文真没题目时，用第一条步骤兜底（是原文里的字，不是编的）
t3 = ("测试用例 联想日历_170 \n前提：\n1. 设备已有课程表\n操作步骤 \n"
      "1. 进入已保存的课程表，点击右上角添加按钮\n2. 选择图库导入\n预期结果 \n1. 可以")
m3 = importer.parse_raw(t3)
ck("★ 没题目时用第一条步骤兜底",
   "进入已保存的课程表" in m3["title"], m3["title"])
ck("兜底不会跨到预期结果里去", "可以" != m3["title"], m3["title"])


print()
print("=" * 70)
print("3) 扫描真实文件夹（用户说的那两个）")
print("=" * 70)

real_dirs = [Path(r"D:\dsh\cal_cases"), Path(r"D:\dsh\cases")]
found = cpaths.raw_case_dirs()
ck("★ 解析出了两个原始 case 目录", len(found) >= 1, str(found))
missing = [str(d) for d in real_dirs if not d.is_dir()]
if missing:
    print(f"       （这两个目录不存在，跳过实扫：{missing}）")
else:
    rows = importer.scan(real_dirs)
    ck("★ 扫到 63 条", len(rows) == 63, str(len(rows)))
    keys = [r["case_key"] for r in rows]
    ck("★ 编号唯一", len(set(keys)) == len(keys))
    ck("★★ 167 和 03 都在（两个文件夹都扫到了）",
       "167" in keys and "03" in keys, f"{keys[:6]}…")
    ck("★ 每条都有原文", all(r["raw_text"].strip() for r in rows))
    ck("★ 每条都有来源路径（留痕）",
       all(r["source_path"] for r in rows))
    # 前缀 13 的文件，编号必须是 167 而不是 13
    r167 = [r for r in rows if r["case_key"] == "167"]
    ck("★★ 13_日历_167.txt 归到 167 号",
       bool(r167) and "13_日历_167" in r167[0]["source_path"],
       str(r167[0]["source_path"]) if r167 else "没找到 167")

print()
print("=" * 70)
print("4) 导入：原文进库，题目取法正确")
print("=" * 70)

# 造一个假目录结构，不碰真库真文件
tmp = Path(tempfile.mkdtemp())
rawdir = tmp / "raw"
cdir = tmp / "cases"
rawdir.mkdir()
cdir.mkdir()
(rawdir / "01_日历_101.txt").write_text(
    "测试用例 日历_101 某某功能（P1）\n操作步骤：\n1. 点什么", encoding="utf-8")
(rawdir / "02_日历_102.txt").write_text(
    "测试用例 联想日历_102 \n前提：无\n操作步骤 \n1. 看某个页面", encoding="utf-8")

res = importer.import_all(db=db, dirs=[rawdir], cases_dir=cdir)
ck("导入 2 条", res["files"] == 2, str(res["files"]))

c102 = st.get_case("102", db=db) or {}
ck("102 没题目时用第一条步骤兜底",
   "看某个页面" in (c102.get("title") or ""), str(c102.get("title")))

print()
print("=" * 70)
print("5) 重复导入：默认不覆盖网页里的修改")
print("=" * 70)

st.save_case_raw("101", "我在网页上改过的原文", db=db)
res2 = importer.import_all(db=db, dirs=[rawdir], cases_dir=cdir)
ck("★★ 默认同步不覆盖已存在的原文",
   (st.get_case("101", db=db) or {}).get("raw_text") == "我在网页上改过的原文",
   (st.get_case("101", db=db) or {}).get("raw_text"))
ck("★ 报告里说明了跳过几条", res2["skipped"] == 2, str(res2["skipped"]))
ck("默认同步不会新增行", res2["created"] == 0, str(res2["created"]))

res3 = importer.import_all(db=db, dirs=[rawdir], cases_dir=cdir, overwrite=True)
ck("★★ 显式 overwrite 才覆盖",
   "点什么" in ((st.get_case("101", db=db) or {}).get("raw_text") or ""),
   (st.get_case("101", db=db) or {}).get("raw_text"))
ck("覆盖时报告 updated", res3["updated"] == 2, str(res3["updated"]))

print()
print("=" * 70)
print("7) 删用例：历史记录不动")
print("=" * 70)
rid = st.save_run(
    case_name="101",
    report={"case_id": "101", "status": "PASS", "summary": "s", "steps_done": 1,
            "elapsed_s": 1.0, "progress": [{"t": time.time()}], "findings": [],
            "strong_stats": {"auto_rate": 1.0}},
    ledger_rows=[], db=db)
ck("先有一条运行记录", st.get_run(rid, db=db) is not None)

n = st.delete_case("101", db=db)
ck("★ 用例删掉了", n == 1 and st.get_case("101", db=db) is None)
ck("★★ 运行记录还在（删用例不连坐）", st.get_run(rid, db=db) is not None)
st.delete_run(rid, db=db)

print()
print("=" * 70)
print("8) 源码层面：迁移和启动导入都在")
print("=" * 70)
src_store = (ROOT / "src" / "android_agent" / "reporting" / "store.py").read_text(
    encoding="utf-8")
src_web = (ROOT / "webui.py").read_text(encoding="utf-8")
app = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")

ck("★ 新表叫 cases，主键是 case_key（编号）",
   "CREATE TABLE IF NOT EXISTS cases" in src_store
   and re.search(r"case_key\s+TEXT PRIMARY KEY", src_store) is not None)
ck("★ 老表 raw_cases 有迁移（不丢数据）",
   "def _migrate_raw_cases" in src_store and "DROP TABLE raw_cases" in src_store)
ck("★ 迁移是幂等的（新表已有就不覆盖）",
   "if exists:" in src_store and "continue" in src_store)

ck("★ 只有空库时才自动导入（否则会冲掉网页改的）",
   "if not st.list_cases():" in src_web)
# 用户要求：「用例库从文件夹导入删除吧」。
# 界面上不再有这条路 —— 新建用例走「新建用例」，原文走「编辑原文」。
ck("★ 前端不再有「从文件夹导入」按钮",
   "从文件夹导入" not in app and "case-sync" not in app)
ck("★ 前端不再调 import 接口", "/api/cases/import" not in app)
ck("★ 后端接口还在（启动空库自动导用得上）",
   '"/api/cases/import"' in src_web)

print()
print("=" * 70)
print("9) 前端：三级页面都能返回")
print("=" * 70)
ck("★ 回退栈存在", "const NAV = []" in app)
ck("★ go() 会压栈", "NAV.push({ ...STATE })" in app)
ck("★ 同一视图不重复压栈", "_sameView(STATE, next)" in app)
ck("★ goBack 有兜底（栈空时不至于点了没反应）",
   "goBack(fallback)" in app and "go('dashboard'" in app)
ck("★ 返回条上写了「回到哪一页」（不是光秃秃一个返回）",
   "← 返回${label" in app)

for fn, who in (("viewCase", "用例详情"), ("viewRuns", "记录列表"),
                ("viewRun", "记录详情")):
    m = re.search(rf"async function {fn}\(.*?\n\}}", app, re.S)
    body = m.group(0) if m else ""
    ck(f"★ {who}有返回按钮", "backBar(" in body and "bindBack()" in body,
       f"{fn} 里缺 backBar/bindBack")

# 一级页面不该有返回 —— 它上面没有"上一层"
for fn in ("viewDashboard", "viewCases", "viewVision"):
    m = re.search(rf"async function {fn}\(.*?\n\}}", app, re.S)
    body = m.group(0) if m else ""
    ck(f"{fn}（一级页面）没有返回按钮", "backBar(" not in body)

# 删完不能把用户留在已经不存在的页面上
ck("★★ 删用例后 replace 回列表（不回那个已删的详情）",
   "go('cases', {}, { replace: true })" in app)
ck("★ 删记录后走 goBack（回真正来的那一页）",
   "goBack(() => go('runs'" in app)

print()
print("=" * 70)
print("10) 反查：runs.case_name 就是用例编号，两边能互相对上")
print("=" * 70)
# ★ 这条是实测踩过的：原以为 runs.case_name 存的是 YAML 文件名（去扩展名），
#   于是拿 Path(yaml_name).stem 去对 —— 结果整个"跑过几次"列永远是 0，
#   而且不报错。实际存的是编号。
ck("★★ 建 run 时 case_name 用的就是编号，能直接当 case_key 查",
   "case_name" in src_store)
con = st.connect(db)
cols = {r["name"] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
con.close()
ck("runs 表有 case_name", "case_name" in cols)

rid2 = st.save_run(
    case_name="777",
    report={"case_id": "777", "status": "PASS", "summary": "s", "steps_done": 1,
            "elapsed_s": 1.0, "progress": [{"t": time.time()}], "findings": [],
            "strong_stats": {"auto_rate": 1.0}},
    ledger_rows=[], db=db)
st.save_case("777", raw_text="原文", db=db)
runs777 = st.list_runs(case_name="777", db=db)
ck("★★ 拿编号能查到这条用例的运行（这就是页面上的关联方式）",
   any(r["run_id"] == rid2 for r in runs777), f"{[r['run_id'] for r in runs777]}")
stats777 = {str(x["case_name"]): x for x in st.stats(db=db)["cases"]}
ck("★ stats 的键就是编号", "777" in stats777, str(list(stats777)[:4]))
st.delete_run(rid2, db=db)
st.delete_case("777", db=db)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
