# -*- coding: utf-8 -*-
"""删用例不得影响已跑过的记录。

## 用户的硬要求

「删除 case 的时候要确保已经跑过的记录没有影响」

## 记录为什么能不受影响（这是设计前提，不是巧合）

记录本身是**自足**的，不依赖用例还活着：

    · 判据   —— 跑的那一刻已经把 YAML 全文快照进 runs.case_yaml
    · 证据   —— log / clicks.jsonl / 截图 / dump.xml 的路径存在
                runs/actions 里，文件在 tmp/ 下，跟用例没关系
    · 断言   —— findings 表
    · 账本   —— actions 表

所以删用例只该动两样：cases 那一行、磁盘上那个 YAML 文件。

## 这个测试在防什么

防的是**将来有人"顺手"加级联**。比如觉得"删用例了那它的记录也没意义了"，
在 delete_case 里补一句 DELETE FROM runs —— 那是不可逆的数据销毁，
而且页面上看不出来（记录列表少几行，像本来就没有）。

所以这里既验行为（行数不变、快照还在、文件还在），
也验源码（删用例的处理路径里不许出现 delete_run / _purge_evidence）。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ANDROID_SKILL_ROOT", str(ROOT / "assets"))
sys.stdout.reconfigure(encoding="utf-8")

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


def counts(db):
    """三张表各有多少行 —— 用来证明删用例压根没碰它们。"""
    con = st.connect(db)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("runs", "actions", "findings")}
    finally:
        con.close()


db = str(Path(tempfile.mkdtemp()) / "t.db")
tmp = Path(tempfile.mkdtemp())

# ── 造一条用例 + 一个 YAML + 两条记录 + 真实证据文件 ──
CDIR = tmp / "cases"
CDIR.mkdir()
YAML_TEXT = ("case_id: '888'\ntitle: 删用例安全测试\n"
             "steps:\n- id: s0\n  desc: 点一下\n  landing: activity=.A\n")
(CDIR / "联想日历_888_删用例安全测试.yaml").write_text(YAML_TEXT, encoding="utf-8")

st.save_case("888", title="删用例安全测试", raw_text="1. 打开日历\n预期：看到首页",
             db=db)
st.set_case_yaml("888", "联想日历_888_删用例安全测试.yaml", db=db)

# 两个真实的证据文件（截图 + dump）
EV = tmp / "shots" / "run_A" / "step01"
EV.mkdir(parents=True)
shot = EV / "shot.png"
xmlf = EV / "dump.xml"
shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload" * 40)
xmlf.write_text("<hierarchy><node text='x'/></hierarchy>", encoding="utf-8")

LOG = tmp / "run_888.log"
LOG.write_text("第一行\n第二行\n", encoding="utf-8")
LEDGER = tmp / "clicks_888.jsonl"
LEDGER.write_text('{"seq":1}\n', encoding="utf-8")

r1 = st.save_run(
    case_name="888",
    report={"case_id": "888", "status": "PASS", "summary": "第一次跑",
            "steps_done": 1, "elapsed_s": 12.0,
            "progress": [{"t": time.time()}],
            "findings": [], "strong_stats": {"auto_rate": 1.0},
            "case_yaml": YAML_TEXT},
    ledger_rows=[{"seq": 1, "step": "s0", "action": "click", "x": 10, "y": 20,
                  "before_act": "A", "after_act": "B", "changed": 1, "ms": 5,
                  "shot_before": str(shot), "xml_before": str(xmlf)}],
    paths={"log": str(LOG), "ledger": str(LEDGER)},
    case_yaml=YAML_TEXT,
    case_yaml_path=str(CDIR / "联想日历_888_删用例安全测试.yaml"),
    db=db)
r2 = st.save_run(
    case_name="888",
    report={"case_id": "888", "status": "FAIL", "summary": "第二次跑",
            "steps_done": 0, "elapsed_s": 8.0,
            "progress": [{"t": time.time()}],
            "findings": [], "strong_stats": {"auto_rate": 0.5},
            "case_yaml": YAML_TEXT},
    ledger_rows=[{"seq": 1, "step": "s0", "action": "click", "x": 11, "y": 21,
                  "before_act": "A", "after_act": "C", "changed": 1, "ms": 9,
                  "shot_before": str(shot), "xml_before": str(xmlf)}],
    paths={"log": str(LOG), "ledger": str(LEDGER)},
    case_yaml=YAML_TEXT,
    case_yaml_path=str(CDIR / "联想日历_888_删用例安全测试.yaml"),
    db=db)

print("=" * 70)
print("1) 删之前：记录是自足的（判据快照在库里，不靠 YAML 文件）")
print("=" * 70)
d1 = st.get_run(r1, db=db)
d2 = st.get_run(r2, db=db)
ck("两条记录都在", d1 is not None and d2 is not None)
ck("★★ 判据快照在库里（删了文件也还在）",
   len(d1.get("case_yaml") or "") == len(YAML_TEXT),
   f"{len(d1.get('case_yaml') or '')} vs {len(YAML_TEXT)}")
ck("★ 证据路径存在库里", bool(d1["actions"][0].get("shot_before")))
ck("证据文件此刻真实存在", shot.is_file() and xmlf.is_file())

before = counts(db)
print(f"      删之前 runs/actions/findings = {before}")

print()
print("=" * 70)
print("2) 删用例：只动 cases 那一行")
print("=" * 70)
n = st.delete_case("888", db=db)
ck("删掉 1 行", n == 1, str(n))
ck("用例没了", st.get_case("888", db=db) is None)

after = counts(db)
ck("★★★ runs 行数一点没变", after["runs"] == before["runs"],
   f"{after['runs']} vs {before['runs']}")
ck("★★★ actions 行数一点没变", after["actions"] == before["actions"],
   f"{after['actions']} vs {before['actions']}")
ck("★★★ findings 行数一点没变", after["findings"] == before["findings"],
   f"{after['findings']} vs {before['findings']}")

print()
print("=" * 70)
print("3) 删完之后：记录点开看到的东西和之前一样")
print("=" * 70)
d1b = st.get_run(r1, db=db)
d2b = st.get_run(r2, db=db)
ck("★★ 两条记录都还读得到", d1b is not None and d2b is not None)
ck("★★ 状态没变", d1b["status"] == "PASS" and d2b["status"] == "FAIL")
ck("★★★ 判据快照逐字没变", d1b.get("case_yaml") == d1.get("case_yaml"))
ck("★★ 动作账本没少", len(d1b["actions"]) == len(d1["actions"]))
ck("账本内容没变",
   [(a["x"], a["y"]) for a in d1b["actions"]]
   == [(a["x"], a["y"]) for a in d1["actions"]])
ck("★ 摘要没变", d1b["summary"] == d1["summary"])

print()
print("=" * 70)
print("4) 证据文件一个没动")
print("=" * 70)
ck("★★ 截图还在", shot.is_file())
ck("★★ 截图内容没变", shot.read_bytes().startswith(b"\x89PNG"))
ck("★★ dump.xml 还在", xmlf.is_file())
ck("★★ 运行日志还在", LOG.is_file() and "第二行" in LOG.read_text(encoding="utf-8"))
ck("★★ 点击账本还在", LEDGER.is_file())
# ⚠️ 只查**会执行的代码**，不查注释 —— delete_case 的 docstring 里正好写着
#    "不碰磁盘上的 YAML …… 得由调用方显式做"，连文档一起搜会把自己的
#    说明误判成违规（第一次写就踩了）。
_src_store = (ROOT / "src" / "android_agent" / "reporting" /
              "store.py").read_text(encoding="utf-8")
_body = _src_store.split("def delete_case")[1].split("\ndef ")[0]
_in_doc, _real = False, []
for ln in _body.splitlines():
    if ln.count('"""') == 1:
        _in_doc = not _in_doc
        continue
    if not _in_doc and ln.strip() and not ln.strip().startswith("#"):
        _real.append(ln)
_real_src = "\n".join(_real)
ck("★ store.delete_case 不碰磁盘（它只删库里的行）",
   ".unlink(" not in _real_src and "rmtree" not in _real_src,
   f"实际代码里出现了文件删除：{_real_src[:140]}")

print()
print("=" * 70)
print("5) 源码：删用例的路径里不许出现级联删除")
print("=" * 70)
src = (ROOT / "webui.py").read_text(encoding="utf-8")
app = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")

# 抠出 DELETE 里"删一条用例"那一段。
#
# ⚠️ 别用 `if path.startswith("/api/cases/")` 当锚点 —— 那个字符串在
#    do_GET 里也有一份，会从 GET 开始截，把中间 DELETE /api/run/ 那段
#    （含 delete_run / _purge_evidence）一起圈进来，于是自我误判成违规。
#    用这一段独有的注释当边界。
start = src.index("# --- 删一条用例")
tail = src[start:]
end = tail.index("# --- 只删 YAML")
block = tail[:end]

ck("★★★ 删用例那一段**没有** delete_run（否则级联删记录了）",
   "delete_run" not in block)
ck("★★★ 删用例那一段**没有** _purge_evidence（否则连截图一起清了）",
   "_purge_evidence" not in block)
ck("★★★ 删用例那一段**没有** rmtree（否则可能删掉整个证据目录）",
   "rmtree" not in block)
ck("★ 只调了 delete_case", "st.delete_case(key)" in block)
ck("★ 只 unlink 那一个 YAML 文件", "fp.unlink()" in block)

ck("★★ 删之前先记下记录 id，删完再核对（不是事后编数字）",
   "runs_before = st.list_runs" in block and "ids_before" in block)
ck("★★ 接口如实报出 runs_lost / runs_changed",
   '"runs_lost"' in block and '"runs_changed"' in block)

ck("★ 记录详情告诉前端用例还在不在（避免死链）",
   'run["case_exists"]' in src)
ck("★ 前端用例没了就不给「看当前版本」的链接",
   "caseGone" in app and "用例已删除" in app)
ck("★ 删完若自检发现记录异常，前端要报警而不是轻描淡写",
   "runs_lost" in app and "请立刻检查" in app)
ck("★ 确认框里明说记录不受影响",
   "完全不受影响" in app and "一个文件不动" in app)

print()
print("=" * 60)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 60)
sys.exit(1 if FAIL else 0)
