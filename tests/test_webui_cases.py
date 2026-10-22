# -*- coding: utf-8 -*-
"""WebUI 新增的四块能力（用例入口 / 原始 case / 视觉配置 / 仪表盘数据）。

## 为什么要测到"渲染出来"这一步

前端的 bug 最烦的是**静默**：接口返回 200、页面不报错、
只是某一块是空的。所以这里不只测 API，还把 JS 交给无头 Chrome 跑一遍，
确认关键内容真的出现在 DOM 里。

## 需求来源（用户原话）

    1.前端还是要和旧 skills 得一样增加 case 入口。里面可以显示 case 和 yaml 文件
    2.记录详情里面要包含原始 case 信息。这样旧指导师哪个 case 了
    3.前端视觉模型配置也要参考旧 skills 得方式
    4.前端 dashboard 尽可能参考旧 case 得样式吧
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")


TMP = Path(tempfile.mkdtemp(prefix="webui4_"))
DB = TMP / "runs.db"
os.environ["ANDROID_SKILL_ROOT"] = str(ROOT / "assets")

from android_agent.reporting import store as st  # noqa: E402

REPORT = {
    "status": "PASS", "summary": "步骤 1/1，预期 1/1 已验证",
    "case_id": "999", "steps_done": 1, "engine": "langgraph",
    "strong_stats": {"auto_rate": 1.0, "by_code": 1, "by_model": 0},
    "progress": [{"t": 1750000000.0}], "elapsed_s": 12.5,
    "findings": [],
}

print("=" * 70)
print("1) 运行记录能落库、能读回来")
print("=" * 70)

rid = st.save_run(case_name="999", report=REPORT, ledger_rows=[], db=DB)
run = st.get_run(rid, db=DB)
ck("run_id 拿到了", rid > 0)
ck("★ report_json 存下来了", bool(run.get("report_json")))

# 列表页不该拖着一大坨 report_json 走
items = st.list_runs(db=DB)
ck("★ list_runs 不带 report_json（列表页用不上）",
   "report_json" not in items[0])

print()
print("=" * 70)
print("2) 老库升级：没有新列也能读、能补上")
print("=" * 70)
# 造一个"加字段之前"的库。
#
# ⚠️ 这份 DDL 要**和真实老库一致** —— 第一次我图省事只写了 6 列，
#    结果失败原因是 "table runs has no column named case_id"，
#    跟迁移一点关系都没有，纯粹是我把测试样本捏得比现实还残。
#    现在照抄加字段之前的 _SCHEMA。
old_db = TMP / "old.db"
import sqlite3  # noqa: E402

con = sqlite3.connect(str(old_db))
con.executescript("""
CREATE TABLE runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    case_name   TEXT NOT NULL,
    case_id     TEXT,
    case_title  TEXT,
    status      TEXT,
    summary     TEXT,
    steps_done  INTEGER,
    steps_total INTEGER,
    auto_rate   REAL,
    elapsed_s   REAL,
    by_model    INTEGER,
    by_code     INTEGER,
    engine      TEXT,
    started_at  TEXT,
    started_ts  REAL,
    finished_at TEXT,
    ledger_path TEXT,
    log_path    TEXT,
    llm_path    TEXT,
    report_path TEXT,
    report_json TEXT
);
CREATE TABLE actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER,
    seq       INTEGER,
    step      TEXT,
    action    TEXT,
    rid       TEXT,
    text      TEXT,
    x         INTEGER,
    y         INTEGER,
    before_act TEXT,
    after_act  TEXT,
    changed    INTEGER,
    ms         REAL,
    ms_click   REAL,
    ms_observe REAL,
    why        TEXT,
    perm       TEXT,
    error      TEXT,
    shot_before TEXT,
    shot_after  TEXT,
    xml_before  TEXT,
    xml_after   TEXT
);
CREATE TABLE findings (
    finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER,
    fid     TEXT,
    kind    TEXT,
    descr   TEXT,
    status  TEXT,
    actual  TEXT
);
""")
con.execute("INSERT INTO runs(case_name, status) VALUES('old','PASS')")
con.commit()
con.close()

ok_old = True
try:
    old = st.get_run(1, db=old_db)
except Exception as e:  # noqa: BLE001
    ok_old = False
    old = {}
    print(f"          {type(e).__name__}: {e}")
ck("★ 老库能打开", ok_old)
ck("老记录能读出 run_dir 列（升级后新增）", "run_dir" in old)

# 升级后再存新记录，必须能写进去
try:
    rid2 = st.save_run(case_name="new", report=REPORT, ledger_rows=[],
                       paths={"run_dir": "D:/x/run1"}, db=old_db)
    ck("★ 升级后的老库能写记录", st.get_run(rid2, db=old_db)["run_dir"] == "D:/x/run1")
except Exception as e:  # noqa: BLE001
    ck("★ 升级后的老库能写记录", False, f"{type(e).__name__}: {e}")

# 迁移要幂等（每次 connect 都跑）
for _ in range(3):
    st.connect(old_db).close()
ck("迁移幂等（连开 4 次不炸）", True)

print()
print("=" * 70)
print("3) 视觉配置：读写、脱敏、留空保留 key")
print("=" * 70)
fake_root = TMP / "fakeroot"
(fake_root / "storage").mkdir(parents=True)
os.environ["ANDROID_SKILL_ROOT"] = str(fake_root)
for m in [k for k in sys.modules if k.startswith("webui")]:
    del sys.modules[m]
sys.path.insert(0, str(ROOT))
import webui  # noqa: E402

ck("没有配置文件时读成空配置（不报错）",
   webui._load_vision()["base_url"] == "")

webui._save_vision({"base_url": "https://api.example.com", "model": "m1",
                    "api_key": "sk-1234567890abcdef"})
conf = webui._load_vision()
ck("存盘后再读回来", conf["model"] == "m1", conf["model"])
ck("★ 配置写到指定位置",
   (fake_root / "storage" / "vision.json").is_file())

masked = webui._mask("sk-1234567890abcdef")
ck("★ api_key 脱敏（首4末4）",
   masked.startswith("sk-1") and masked.endswith("cdef") and "*" in masked,
   masked)
ck("★ 脱敏后不含明文中间段", "234567890abc" not in masked, masked)
ck("短串整体打码", webui._mask("abc") == "***", webui._mask("abc"))
ck("空串还是空串", webui._mask("") == "")

print()
print("=" * 70)
print("4) 仪表盘口径：按用例算，不按运行次数算")
print("=" * 70)
dash_db = TMP / "dash.db"
# 同一用例跑 3 次（前两次 FAIL 最后一次 PASS），另一个用例跑 1 次 FAIL
for status in ("FAIL", "FAIL", "PASS"):
    st.save_run(case_name="A", report={**REPORT, "status": status},
                ledger_rows=[], db=dash_db)
st.save_run(case_name="B", report={**REPORT, "status": "FAIL"},
            ledger_rows=[], db=dash_db)
s = st.stats(db=dash_db)
cases = {c["case_name"]: c for c in s["cases"]}
ck("两个用例", len(cases) == 2, str(list(cases)))
ck("★ A 的最近结论是最后一次（PASS）",
   cases["A"]["last_status"] == "PASS", cases["A"]["last_status"])
ck("A 跑了 3 次", cases["A"]["n"] == 3)
ck("B 的最近结论是 FAIL", cases["B"]["last_status"] == "FAIL")
ck("★ days 趋势存在（仪表盘三天柱要用）", isinstance(s.get("days"), list),
   str(s.get("days")))

print()
print("=" * 70)
print("5) 前端源码里确实接上了这四块")
print("=" * 70)
app = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")

ck("★ 侧栏有「用例库」入口", 'data-view="cases"' in html)
ck("★ 侧栏有「视觉模型」入口", 'data-view="vision"' in html)
ck("★ 有 viewCases", "async function viewCases" in app)
ck("★ 有 viewCase（原始 case + YAML 一体页）", "async function viewCase(" in app)
ck("★ 有 viewVision", "async function viewVision" in app)
ck("★ 详情页有 caseInfoHtml", "function caseInfoHtml" in app)
ck("★ 详情页调用了 caseInfoHtml", "${caseInfoHtml(run)}" in app)
ck("★ 仪表盘用了 panel/seg 布局", "dash-seg" in app and "seg-count" in app)
ck("★ 仪表盘画了环形图", 'class="ring"' in app)
ck("★ 仪表盘三天柱", 'class="bar-col"' in app)
ck("★ 仪表盘口径写明「按用例最近一次」", "按用例最近一次结论计" in app)
ck("★ 不引图表库（自己画 SVG）",
   "echarts.init" not in app and "echarts" not in html.lower()
   and not (ROOT / "webui" / "echarts.min.js").exists())


def _func_body(src: str, name: str) -> str:
    """按大括号配平切出一个函数的**代码**（去掉注释行）。

    ⚠️ 不能用 `split("\\nfunction ")` 那种切法 —— 切出来的范围会
       一路吃到下一个函数里，于是我第一次把 viewRuns 的 await api()
       当成了 caseInfoHtml 的问题，误判成"没修"。
       也不能留注释：这条规则我是**写在注释里**解释为什么改的，
       而注释里正好就提到了 /api/case/，不剔掉永远过不了。
    """
    i = src.find(f"function {name}")
    assert i >= 0, name
    j = src.find("{", i)
    depth, end = 0, len(src)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    body = src[i:end]
    # 去注释（行注释 + 块注释）
    import re as _re
    body = _re.sub(r"/\*.*?\*/", "", body, flags=_re.S)
    body = "\n".join(ln for ln in body.splitlines()
                     if not ln.strip().startswith(("*", "//")))
    return body


_ci = _func_body(app, "caseInfoHtml")
ck("★ 详情页不拿当前文件冒充快照",
   "/api/case/" not in _ci and "await api(" not in _ci,
   "caseInfoHtml 里不该有任何 fetch")
ck("★★ 记录详情不再内嵌 YAML 内容（只给链接跳用例详情）",
   "run.case_yaml" not in _ci,
   "用户要求做减法：记录页只显示原始用例，YAML 给个链接")
ck("★ key 留空的语义写在界面上", "留空保存 = 保留原值" in app)

print()
print("=" * 70)
print("6) 真的渲染出来（无头 Chrome）")
print("=" * 70)
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
if not CHROME.is_file():
    print("  (没装 Chrome，跳过渲染检查)")
else:
    import subprocess  # noqa: E402
    import threading  # noqa: E402
    import time  # noqa: E402
    import urllib.request  # noqa: E402
    from http.server import ThreadingHTTPServer  # noqa: E402

    # 起一个临时服务（用它自己的库），避免依赖用户正开着的那个
    os.environ["ANDROID_SKILL_ROOT"] = str(fake_root)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{port}"

    def render(js: str, label: str) -> str:
        page = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
        page = page.replace(
            '<script src="/app.js"></script>',
            '<script src="/app.js"></script><script>'
            f'window.addEventListener("load",function(){{setTimeout(function(){{{js}}},500);}});'
            '</script>')
        (ROOT / "webui" / f"_t_{label}.html").write_text(page, encoding="utf-8")
        try:
            r = subprocess.run(
                [str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--virtual-time-budget=9000", "--dump-dom",
                 f"{base}/_t_{label}.html"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=90)
            dom = r.stdout
        finally:
            (ROOT / "webui" / f"_t_{label}.html").unlink(missing_ok=True)
        m = re.search(r'<main id="main">(.*?)</main>', dom, re.S)
        return m.group(1) if m else ""

    # ★ 自己造一条用例再渲染。
    #   原来这里断言页面上有「联想日历_168」，但本测试的 root 指向
    #   临时空目录 fake_root —— 库里一条用例都没有，能不能过
    #   全看那台机器上别的库恰不巧有数据。现在不依赖外部数据了。
    st.save_case("168", title="联想日历_168_图库导入入口",
                 raw_text="操作步骤\n1. 点更多\n预期结果\n1. 打开课程表",
                 source_path="(测试夹具)")

    body = render('go("cases")', "cases")
    ck("★ 用例库页面渲染出来了", "用例库" in body, body[:100])
    ck("★ 列表里真的渲染出用例了", "联想日历_168" in body, body[:200])

    body = render('go("vision")', "vision")
    ck("★ 视觉配置页渲染出来了", "视觉模型" in body, body[:100])
    ck("★ 有 Base URL / Model / API Key 三个字段",
       all(k in body for k in ("Base URL", "Model", "API Key")))
    ck("★ 有测试连接按钮", "测试连接" in body)

    body = render('go("dashboard")', "dash")
    ck("★ 仪表盘渲染出来了", "总览" in body, body[:100])

    srv.shutdown()

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
