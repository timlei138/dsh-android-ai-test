# -*- coding: utf-8 -*-
"""一次运行 = 一个独立目录。

用户的要求：「每个测试记录用 uuid 作为文件夹吧。把所有跑这个 case 生成的
截图、xml 都放到这个目录下。」

## 这条规矩为什么重要（不是洁癖）

早先产物是平铺在 tmp/ 下、按 `<case_id>` 命名的：

    tmp/report_167.json / run_167.log / clicks_167.jsonl / shots/step03/shot.png

同一个用例跑两次就**互相覆盖**。库里 10 条 167 记录全都记着
`tmp/shots/step04/shot.png`，而磁盘上那份其实是**最后一次**的 ——
前面 9 条点开看到的是别的运行的图，而且路径和文件名都对，根本看不出来。

所以这里要钉住三件事：
  1. run_tag 里带 uuid（同一秒跑两次也不会撞）
  2. 目录名带用例编号（人翻文件能认出来）
  3. 报告能带着 run_dir 出去（落库后能反查现场）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


from android_agent.agent import runner  # noqa: E402
from android_agent.agent.nodes import SubagentReport  # noqa: E402

print("\n=== 1) 目录名带 uuid，不会撞 ===")
t1 = runner._run_tag()
t2 = runner._run_tag()
ck("两次调用不相等", t1 != t2, f"{t1} vs {t2}")
ck("长度 >= 8（uuid 前缀 + 时间戳）", len(t1) >= 8, t1)
# 形如 8 位十六进制 + 下划线 + 时间戳
head = t1.split("_")[0]
ck("前段是 8 位十六进制 uuid 前缀",
   len(head) == 8 and all(c in "0123456789abcdef" for c in head), head)
ck("后段是年月日_时分秒",
   len(t1.split("_", 1)[1]) == 15, t1.split("_", 1)[1])

print("\n=== 2) 报告能带 run_dir 出去 ===")
r = SubagentReport(case_id="176", status="PASS", summary="s")
ck("默认空串（不是 None，落库不会炸）", r.run_dir == "")
r.run_dir = "/tmp/runs/abc_20240101_120000_176"
dumped = r.model_dump()
ck("进得了 model_dump（否则落库拿不到）", dumped.get("run_dir") == r.run_dir)

print("\n=== 3) 崩溃日志字段也在报告里 ===")
ck("crash_log 默认空串", r.crash_log == "")
r.crash_log = "FATAL EXCEPTION: main"
ck("crash_log 进得了 model_dump",
   r.model_dump().get("crash_log") == "FATAL EXCEPTION: main")

print("\n=== 4) 产物路径全部落在 run_dir 之下（不散在 tmp 根）===")
src = Path(runner.__file__).read_text(encoding="utf-8")
# 这几处旧布局如果回来了，两条运行就会互相覆盖 —— 直接拦住。
for bad in ("f\"report_{spec.case_id}.json\"",
            "f\"clicks_{spec.case_id}.jsonl\"",
            "f\"child_{spec.case_id}.db\""):
    ck(f"没有回到旧布局 {bad}", bad not in src)
ck("report_path 在 run_dir 下", 'report_path = run_dir / "report.json"' in src)
ck("ledger 在 run_dir 下", 'ledger_path = run_dir / "clicks.jsonl"' in src)
ck("证据目录在 run_dir 下", 'evidence_dir=(run_dir / "shots")' in src)

print("\n=== 5) 落库表里有 run_dir 和 crash_log ===")
from android_agent.reporting import store  # noqa: E402
cols = {c for t, c, _ty in store._ADDED_COLUMNS if t == "runs"}
ck("runs 表加了 run_dir", "run_dir" in cols, str(cols))
ck("runs 表加了 crash_log", "crash_log" in cols, str(cols))

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
