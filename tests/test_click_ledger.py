# -*- coding: utf-8 -*-
"""验证点击账本：每次点击都留下可排查的完整记录。

用户要求："每一步点击了啥都要增加记录，好方便排查。"

判据（这就是"方便排查"的定义）：
  · 能看到**点了哪个控件**（rid/text），不是只有一个像素坐标
  · 能看到**点之前/之后是什么页面**
  · 能看到**画面变没变**（区分"点了没反应"和"点了但没跳"）
  · 能看到**各段耗时**（找出卡在点击还是卡在观察）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from android_agent.device.ledger import ClickLedger, target_from_screen  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def ck(name, ok, detail=""):
    CHECKS.append((name, ok, detail))


tmp = Path(tempfile.mkdtemp()) / "clicks.jsonl"
lg = ClickLedger(tmp)

# ── 1. 基本记账 ───────────────────────────────────────────────────
r = lg.record(step="s3", action="click",
              target={"rid": "save_view", "text": "完成", "c": [1777, 202]},
              before=".timetable.management.EditTimetableActivity",
              after=".timetable.management.EditTimetableActivity",
              ms=812.0, changed=False, why="点完成保存")
ck("记账返回记录", r.get("seq") == 1)
ck("文件已落盘", tmp.exists() and tmp.stat().st_size > 0)

# ── 2. 落盘内容完整 ───────────────────────────────────────────────
saved = json.loads(tmp.read_text(encoding="utf-8").strip().splitlines()[0])
for k in ("seq", "t", "step", "target", "before", "after", "ms", "changed", "why"):
    ck(f"记录含 {k}", k in saved)
ck("控件 rid 记下来了", saved["target"].get("rid") == "save_view")
ck("控件文案记下来了", saved["target"].get("text") == "完成")
ck("★ 前后页面都记了",
   saved["before"].endswith("EditTimetableActivity")
   and saved["after"].endswith("EditTimetableActivity"))
ck("★ 记了画面有没有变", saved["changed"] is False)

# ── 3. 多条追加，不覆盖 ───────────────────────────────────────────
lg.record(step="s4", target={"rid": "cv_empty_content"},
          before=".timetable.display.TimetableActivity",
          after=".timetable.course.EditCourseActivity", ms=500.0, changed=True)
lines = [x for x in tmp.read_text(encoding="utf-8").splitlines() if x.strip()]
ck("★ 第二次点击追加（不覆盖第一条）", len(lines) == 2, f"实际 {len(lines)} 行")
ck("seq 递增", json.loads(lines[1])["seq"] == 2)

# ── 4. 人读的渲染 ─────────────────────────────────────────────────
txt = lg.render()
ck("渲染含控件文案「完成」", "完成" in txt)
ck("渲染含变化标记", "是" in txt and "否" in txt)
ck("渲染含表头列", "控件" in txt and "前" in txt and "后" in txt)

# ── 5. 坐标反查控件（账本可读性的关键）────────────────────────────
class FakeScreen:
    clickable = [
        {"r": "button1", "t": "同意", "c": [1795, 1583]},
        {"r": "button2", "t": "退出", "c": [1244, 1583]},
        {"r": "save_view", "t": "完成", "c": [1777, 202]},
    ]


t1 = target_from_screen(FakeScreen(), 1795, 1583)
ck("★ 坐标能反查到控件 rid", t1.get("rid") == "button1", str(t1))
ck("★ 坐标能反查到控件文案", t1.get("text") == "同意", str(t1))

t2 = target_from_screen(FakeScreen(), 300, 900)
ck("点空白处时只记坐标",
   not t2.get("rid") and not t2.get("text") and t2.get("c") == [300, 900],
   str(t2))

# ── 6. 空账本不炸 ─────────────────────────────────────────────────
empty = ClickLedger(Path(tempfile.mkdtemp()) / "e.jsonl")
ck("空账本能渲染", "还没有" in empty.render())
ck("空账本 rows 是空列表", empty.rows() == [])

# ── 7. runner / direct 接线检查 ───────────────────────────────────
direct_src = (ROOT / "src/android_agent/device/direct.py").read_text(encoding="utf-8")
runner_src = (ROOT / "src/android_agent/agent/runner.py").read_text(encoding="utf-8")
ck("★ DirectDevice 支持 ledger_path", "ledger_path" in direct_src)
ck("★ act 里会记账", "self._ledger.record(" in direct_src)
ck("★ 记账前反查控件", "target_from_screen(" in direct_src)
ck("★ runner 创建账本文件", "clicks_" in runner_src)
ck("★ 日志里会打账本", "点击账本" in runner_src)

bad = [c for c in CHECKS if not c[1]]
print()
for name, ok, detail in CHECKS:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail else ""))
print()
print(f"  {len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
sys.exit(1 if bad else 0)
