# -*- coding: utf-8 -*-
"""强证据的值必须来自真实记录（不许凭空捏造）。

配套 `tools/check_evidence_provenance.py`。

## 背景

用户要求：「所有强证据的值必须来自真实的记录。不能凭空捏造。」

YAML 里的 `rid` / `activity` / `package` 是代码硬判的唯一依据。
写一个从没出现过的值，后果分两种，都很难查：
  · 判据永远不成立 -> 功能明明没问题却 FAIL
  · 判据永远成立   -> 假 PASS

历史实锤：`EditCourseActivity` / `btnDelete` / `TimetableGuideActivity`
曾被写进 YAML，但**在任何真实 dump/日志里都找不到** ——
因为那几条路径从没跑通过，值是照着"应该叫什么"推的。

## 这个测试管什么

  1. 校验脚本本身能跑、能识别编造的值（正样本）
  2. 现有 YAML 的"没找到"清单是**已知的**，不会悄悄变多
     （多了说明又有人凭想象写了新值）
"""

import glob
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_evidence_provenance.py"

FAIL = 0
PASS = 0


def ck(name, cond, extra=""):
    global FAIL, PASS
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


print("\n=== 1) 校验脚本存在且能跑 ===")
ck("tools/check_evidence_provenance.py 存在", TOOL.is_file())

r = subprocess.run([sys.executable, str(TOOL)],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", cwd=str(ROOT))
out = (r.stdout or "") + (r.stderr or "")
ck("脚本能正常执行（不崩）", "证据库" in out, out[:200])
ck("脚本报告了核对条数", "强证据核对" in out)

print("\n=== 2) 证据库真的取自真实记录 ===")
m = re.search(r"证据库：(\d+) 个资源名 / (\d+) 个 activity", out)
ck("找到了资源名/activity 统计", bool(m), out[:120])
if m:
    n_rid, n_act = int(m.group(1)), int(m.group(2))
    ck("★ 资源名数量合理（>200，说明真扫了 dump）", n_rid > 200, str(n_rid))
    ck("★ activity 数量合理（>5，说明读了运行日志）", n_act > 5, str(n_act))

print("\n=== 3) activity 来源必须是运行日志，不是 dump.xml ===")
src = TOOL.read_text(encoding="utf-8")
ck("★ 脚本读了 .log（activity 只在那里）", "*.log" in src, "没读日志就拿不到 activity")
ck("★ 脚本认 act= 这种写法", "act=" in src or r"act\=" in src)

print("\n=== 4) 系统框资源要豁免，被测 App 的不能豁免 ===")
ck("★ 豁免 android:id/", "android:id/" in src)
# 不能把整个 com.zui.* 豁免掉 —— 被测 App 就是 com.zui.calendar
ck("★★ 没有豁免 com.zui.（否则整个 App 的 rid 全放过）",
   '"com.zui."' not in src.split("SYS_PREFIX")[1][:200]
   if "SYS_PREFIX" in src else False)

print("\n=== 5) 已知的「无出处」清单不能变多 ===")
# 当前已知没出处的 3 条：都是因为执行器还没真正跑到那些页面。
# 这个清单**只许变少**（补采证据后），变多说明又有人凭想象写了值。
KNOWN_MISSING = {
    (".timetable.course.EditCourseActivity", "176"),
    (".timetable.course.EditCourseActivity", "183"),
    ("btnDelete", "183"),
}
found = set()
for m2 in re.finditer(r"✗ (\S+\.yaml)\n\s+?(rid|activity) `([^`]+)`", out):
    fname, _, val = m2.group(1), m2.group(2), m2.group(3)
    key = next((k for k in KNOWN_MISSING if k[0] == val and k[1] in fname), None)
    if key:
        found.add(key)
    else:
        ck(f"★ 没有新的凭空值：{val} ({fname[-20:]})", False,
           "这条不在已知清单里 —— 新写的值必须有真实出处")
total_missing = out.count("✗")
ck("★★ 没出处的总数不超过已知清单（3 条）",
   total_missing <= len(KNOWN_MISSING),
   f"实际 {total_missing} 条，已知 {len(KNOWN_MISSING)} 条")

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
if not FAIL:
    print(f"  （已知待补证据 {total_missing} 处，见脚本输出）")
print("=" * 70)
sys.exit(1 if FAIL else 0)
