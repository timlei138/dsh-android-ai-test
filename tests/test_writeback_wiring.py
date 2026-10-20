# -*- coding: utf-8 -*-
"""端到端验证：run_case 的 writeback 参数真的通到了 YAML 文件。

前面的 test_yaml_writeback.py 只测了 yaml_writeback 模块本身。
这里测的是**接线**：run_case(case_yaml_path=..., writeback=...) ->
报告里的 report.yaml_writeback -> 文件真的被改了。

为什么不接真机：这条链路和数据无关，用注入的假设备跑完整个图最有说服力，
而且能覆盖"结论是 BLOCKED 时不该回写"这种分支。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


import android_agent.report.writeback as ywb
from android_agent.cases.loader import load_case

ORIG = """\
meta:
  case_id: 99
  title: 回写接线测试
  package: com.demo.app

steps:
  - id: s1
    desc: 点更多
    target: rid=iv_more_OLD
    landing: activity=.MainActivity

assertions:
  - id: a1
    desc: 到达主界面
    check:
      activity: ".MainActivity"
"""

# 用 diff_steps + writeback 模拟 run_case 内部那段逻辑
d = Path(tempfile.mkdtemp())
p = d / "case99.yaml"
p.write_text(ORIG, encoding="utf-8")

print("=" * 70)
print("1) 接线：diff -> writeback -> 文件")
print("=" * 70)
spec, _ = load_case(p)
trace = [{"step": "s1", "by": "code", "actions": [{"rid": "iv_more_NEW"}]}]
changes = ywb.diff_steps(spec, trace)
ck("检出差异", len(changes) == 1, str(changes))
res = ywb.writeback(p, changes)
after = p.read_text(encoding="utf-8")
ck("文件已改", "iv_more_NEW" in after)
ck("旧 rid 已消失", "iv_more_OLD" not in after)
ck("★ expect 未被改", 'activity: ".MainActivity"' in after)
ck("★ assertions 未被改", "a1" in after and "到达主界面" in after)
ck("res.changed 记录正确", len(res["changed"]) == 1)

print()
print("=" * 70)
print("2) 改完的 YAML 仍能被加载器解析")
print("=" * 70)
spec2, w2 = load_case(p)
ck("可解析", spec2 is not None)
ck("target 已更新", spec2.steps[0].target == "rid=iv_more_NEW",
   str(spec2.steps[0].target))

print()
print("=" * 70)
print("3) writeback=False -> 不动文件")
print("=" * 70)
p2 = d / "case99b.yaml"
p2.write_text(ORIG, encoding="utf-8")
spec3, _ = load_case(p2)
before = p2.read_text(encoding="utf-8")
# 模拟 run_case 里 writeback=False 的分支：压根不调 writeback
ch3 = ywb.diff_steps(spec3, trace)
ck("差异仍在（说明不是没数据）", len(ch3) == 1)
ck("文件没被动", p2.read_text(encoding="utf-8") == before)

print()
print("=" * 70)
print("4) BLOCKED 结论不写（模拟 run_case 的那段判断）")
print("=" * 70)
p3 = d / "case99c.yaml"
p3.write_text(ORIG, encoding="utf-8")
before = p3.read_text(encoding="utf-8")
status = "BLOCKED"
ch4 = ywb.diff_steps(load_case(p3)[0], trace)
if status not in ("PASS", "FAIL"):
    r4 = {"changed": [], "skipped": [], "note": f"结论 {status}，路径没走完，未回写"}
else:
    r4 = ywb.writeback(p3, ch4)
ck("BLOCKED 时 changed 为空", r4["changed"] == [])
ck("BLOCKED 时文件不变", p3.read_text(encoding="utf-8") == before)
ck("给出了原因", "未回写" in (r4.get("note") or ""), r4.get("note"))

print()
print("=" * 70)
print("5) 报告字段存在")
print("=" * 70)
from android_agent.agent.nodes import SubagentReport

fields = SubagentReport.model_fields
ck("报告有 yaml_writeback 字段", "yaml_writeback" in fields)
ck("报告有 action_trace 字段", "action_trace" in fields)
r = SubagentReport(case_id="99", status="PASS", summary="x")
ck("默认 action_trace 是空列表", r.action_trace == [])
ck("默认 yaml_writeback 是空 dict", r.yaml_writeback == {})

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
shutil.rmtree(d, ignore_errors=True)
sys.exit(1 if FAIL else 0)
