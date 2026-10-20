# -*- coding: utf-8 -*-
"""WebUI 的数据链路：账本 -> 库 -> 页面。

## 为什么要有这条测试

WebUI 最容易坏在**数据连接处**，而且坏得很安静：
页面能打开、表格也在，只是某一列永远是空的 —— 不报错，看不出来。

实测就踩过两次：
  ① 账本里**根本没记截图路径**，于是"证据"那一栏永远没有图；
  ② input/key 动作**根本不进账本**，于是时间轴上凭空少一半动作。

这两处都不是前端 bug，但表现都在前端。所以这里从账本一路验到 API 返回，
而不是只测前端有没有渲染。
"""

from __future__ import annotations

import json
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


print("=" * 70)
print("1) 账本 -> 库")
print("=" * 70)

from android_agent.reporting import store as st  # noqa: E402

_tmpdir = Path(tempfile.mkdtemp(prefix="ui_"))
_db = _tmpdir / "runs.db"

LEDGER = [
    {"seq": 1, "step": "s1", "action": "click",
     "target": {"rid": "button1", "text": "同意", "c": [1795, 1583]},
     "before": "com.zui.calendar.AllInOneActivity",
     "after": "com.android.permissioncontroller.GrantPermissionsActivity",
     "ms": 3610.0, "ms_click": 186.7, "ms_observe": 1606.7, "changed": True,
     "why": "点掉隐私弹窗",
     "shot_before": "tmp/shots/step01/shot.png",
     "shot_after": "tmp/shots/step01/shot.png",
     "xml_before": "tmp/shots/step01/dump.xml",
     "xml_after": "tmp/shots/step01/dump.xml"},
    {"seq": 2, "step": "s1", "action": "input",
     "target": {"rid": "et_schedule_name", "text": "测试课程表", "c": [1000, 500]},
     "before": "EditTimetableActivity", "after": "EditTimetableActivity",
     "ms": 2400.0, "changed": True, "why": "填必填名称",
     "shot_after": "tmp/shots/step02/shot.png"},
    {"seq": 3, "step": "s2", "action": "key:back",
     "target": {"key": "back"},
     "before": "EditTimetableActivity", "after": "TimetableActivity",
     "ms": 1800.0, "changed": True, "why": "返回周视图",
     "shot_after": "tmp/shots/step03/shot.png"},
]

REPORT = {
    "case_id": "168",
    "status": "BLOCKED",
    "summary": "联想日历_168_图库导入入口：步骤 3/3，预期 3/3 已验证",
    "steps_done": 3,
    "engine": "model",
    "elapsed_s": 127.9,
    "progress": [{"t": 1792488000.0, "phase": "prep", "note": "开始"}],
    "strong_stats": {"by_code": 2, "by_model": 2, "auto_rate": 0.5,
                     "fallback_reasons": ["s1 目标不在当前屏：rid=iv_more 没找到"]},
    "findings": [
        {"kind": "assert", "id": "a1", "desc": "可打开课程表页面",
         "status": "PASS", "actual": "[代码判定] activity ✔ TimetableActivity"},
        {"kind": "assert", "id": "a2", "desc": "图库入口可点",
         "status": "FAIL", "actual": "[代码判定] activity ✘ PhotoPicker"},
    ],
}

rid = st.save_run(case_name="168", report=REPORT, ledger_rows=LEDGER,
                  paths={"ledger": "tmp/clicks_168.jsonl",
                         "log": "tmp/run_168.log",
                         "llm": "tmp/llm_168.md"},
                  db=_db)
ck("save_run 返回 run_id", isinstance(rid, int) and rid > 0, str(rid))

run = st.get_run(rid, db=_db)
ck("get_run 拿到运行", run is not None)
ck("状态正确", run["status"] == "BLOCKED", str(run.get("status")))
ck("总步数从 summary 里抠出来了", run["steps_total"] == 3, str(run["steps_total"]))
ck("自动化率落库", abs((run["auto_rate"] or 0) - 0.5) < 1e-6, str(run["auto_rate"]))
ck("代码/模型步数分开存", (run["by_code"], run["by_model"]) == (2, 2),
   f'{run["by_code"]}/{run["by_model"]}')
ck("断言全部落库", len(run["findings"]) == 2, str(len(run["findings"])))

print()
print("2) 三种动作都要进库，且目标字段拆得开")
acts = run["actions"]
ck("动作数 = 账本行数", len(acts) == 3, str(len(acts)))
ck("click 的 rid/text/坐标都拆出来了",
   acts[0]["rid"] == "button1" and acts[0]["text"] == "同意"
   and acts[0]["x"] == 1795 and acts[0]["y"] == 1583,
   f'{acts[0]["rid"]}/{acts[0]["text"]}/{acts[0]["x"]},{acts[0]["y"]}')
ck("★ input 动作在库里（原来根本不记账）",
   any(a["action"] == "input" for a in acts),
   "账本丢了输入动作，页面上会凭空少一半")
ck("★ key:back 动作在库里（原来根本不记账）",
   any(a["action"] == "key:back" for a in acts),
   "账本丢了按键，模型按返回在页面上看不见")
ck("changed 转成 0/1", all(a["changed"] in (0, 1) for a in acts),
   str([a["changed"] for a in acts]))
ck("耗时字段都在", all(a["ms"] is not None for a in acts))

print()
print("3) 证据路径必须落库（否则页面上全是破图）")
ck("★ 至少一条动作有截前图", any(a["shot_before"] for a in acts))
ck("★ 每条动作都有截后图", all(a["shot_after"] for a in acts),
   f'缺: {[a["seq"] for a in acts if not a["shot_after"]]}')
ck("xml 路径也记了", any(a.get("xml_before") or a.get("xml_after")
                        for a in acts))

print()
print("4) 列表与统计")
runs = st.list_runs(db=_db)
ck("list_runs 拿到 1 条", len(runs) == 1, str(len(runs)))
ck("列表里不带 report_json（省带宽）", "report_json" not in runs[0])
s = st.stats(db=_db)
ck("stats 总数正确", s["total"] == 1, str(s["total"]))
ck("stats 按状态分组", s["by_status"].get("BLOCKED") == 1, str(s["by_status"]))
ck("stats 有每个用例的最近结果",
   len(s["cases"]) == 1 and s["cases"][0]["last_status"] == "BLOCKED",
   str(s["cases"]))

print()
print("5) 历史不再被覆盖 —— 同一个用例跑第二次，两条都在")
# 第一次 a2 是 FAIL，第二次修好了变 PASS —— 这正是"对比"要回答的问题
_rep2 = {**REPORT, "status": "PASS",
         "findings": [REPORT["findings"][0],
                      {**REPORT["findings"][1], "status": "PASS",
                       "actual": "[代码判定] activity ✔ PhotoPicker"}]}
rid2 = st.save_run(case_name="168", report=_rep2,
                   ledger_rows=[], paths={}, db=_db)
runs2 = st.list_runs(case_name="168", db=_db)
ck("跑两次有两条历史", len(runs2) == 2, str(len(runs2)))
ck("新的在前", runs2[0]["run_id"] == rid2 and runs2[1]["run_id"] == rid)
cmp = st.compare_runs(rid, rid2, db=_db)
ck("compare 能看出状态翻转", cmp.get("status_changed") is True, str(cmp.get("status_changed")))
ck("compare 能看出断言变化",
   any(f["fid"] == "a2" for f in (cmp.get("flipped") or [])),
   str(cmp.get("flipped")))

print()
print("6) 路径穿越必须被挡（WebUI 服务端的校验逻辑）")
sys.path.insert(0, str(ROOT))
import webui  # noqa: E402

_roots = [p.resolve() for p in webui._evidence_roots()]
_bad = (Path(_roots[0]) / ".." / ".." / ".." / "Windows" / "win.ini").resolve()
_hit = any(str(_bad).startswith(str(r)) for r in _roots)
ck("仓库外的路径不会被当成证据", not _hit, str(_bad))
_good = (_roots[0] / "step01" / "shot.png").resolve()
ck("证据目录内的路径放行",
   any(str(_good).startswith(str(r)) for r in _roots), str(_good))

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
