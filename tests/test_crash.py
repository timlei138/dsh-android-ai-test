"""崩溃硬终止路径的单测（不碰设备）。

对应 observe.py 的真实行为：检测到被测包崩溃时会 terminate_session，
此后 observe/act 全部被拒。图必须认这个信号并直接 BLOCKED，
而不是反复重试到轮次上限（那会报出误导性的"未收敛"）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import android_agent.agent.nodes as ca  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


class CrashDev:
    """第一次 observe 就报崩溃终止（模拟 App 启动即崩）。"""

    def __init__(self):
        self.observes = 0
        self.acts = 0
        self.finished = None

    def observe_raw(self):
        self.observes += 1
        return {
            "activity": ".MainActivity", "package": "com.zui.calendar",
            "terminate": True,
            "terminate_reason": "com.zui.calendar 崩溃 (FATAL EXCEPTION)",
            "crash": {"scope": "target", "type": "java.lang.NullPointerException"},
            "next": [{"r": "x", "t": "t", "c": [1, 2]}],
            "event_ids": ["e1"],
        }

    def act(self, *a, **k):
        self.acts += 1
        raise AssertionError("崩溃后不应再 act")

    def finding(self, *a, **k):
        return {"ok": True}

    def finding_no_evidence(self, status, expect, actual):
        """session.py 的 --event-id 是 append 且默认 []，省略即合法。"""
        return self.finding(status, "", expect, actual)
    def finish(self, status, summary):
        self.finished = {"status": status, "summary": summary}
        return {"ok": True}

    def shell(self, command, timeout=60):
        return ""


TASK = {
    "case_id": "crash", "title": "崩溃用例", "package": "com.zui.calendar",
    "steps": [{"id": "s1", "desc": "打开页面", "landing": "activity=X"},
              {"id": "s2", "desc": "第二步"}],
    "asserts": [{"id": "a1", "desc": "预期一"}],
    "knowledge_files": [],
}

print("=== 崩溃硬终止 ===")
dev = CrashDev()
rp = Path(__file__).resolve().parents[1] / "tmp" / "_crash_test.json"
ca.set_context(ca.ChildContext(device=dev, skill_root=".", python="py",
                               report_path=str(rp)))
app = ca.build_child_graph(
    ca.ChildContext(device=dev, skill_root=".", python="py",
                    report_path=str(rp)),
    SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
cfg = {"configurable": {"thread_id": "crash1"}}

r = app.invoke({"task": TASK, "step_index": 0, "round_in_step": 0,
                "max_rounds_per_step": 8, "findings": [], "issues": [],
                "visited": [], "session_id": 1}, cfg)

check("崩溃后不再向 AI 提问（无 interrupt）", not r.get("__interrupt__"),
      repr(r.get("__interrupt__"))[:80])
check("状态为 BLOCKED", r.get("status") == "BLOCKED", str(r.get("status")))
check("只 observe 一次，未重试", dev.observes == 1, f"observes={dev.observes}")
check("崩溃后零次 act", dev.acts == 0, f"acts={dev.acts}")
check("finish 记为 BLOCKED", (dev.finished or {}).get("status") == "BLOCKED",
      str(dev.finished))

rep = json.loads(rp.read_text(encoding="utf-8"))
check("报告 status=BLOCKED", rep["status"] == "BLOCKED", rep["status"])
check("报告含崩溃原因", any("崩溃" in i or "crash" in i.lower()
                            for i in rep["issues"]), str(rep["issues"])[:100])
check("coverage_ok=False", rep["coverage_ok"] is False)


class RejectedDev:
    """会话已被锁死：observe 直接抛错（模拟 session 475 那种处境）。"""

    def __init__(self):
        self.observes = 0
        self.acts = 0

    def observe_raw(self):
        self.observes += 1
        raise ca.ToolError(
            "Session 1 has incomplete recording. Resolve it and finish "
            "ERROR/BLOCKED; do not replay actions.",
            executed=False, returncode=3)

    def act(self, *a, **k):
        self.acts += 1
        raise AssertionError("会话锁死后不应 act")

    def finding(self, *a, **k):
        return {"ok": True}

    def finish(self, *a, **k):
        return {"ok": True}

    def shell(self, command, timeout=60):
        return ""


print("\n=== 会话锁死（ToolError）不重试 ===")
dev2 = RejectedDev()
rp2 = Path(__file__).resolve().parents[1] / "tmp" / "_locked_test.json"
app2 = ca.build_child_graph(
    ca.ChildContext(device=dev2, skill_root=".", python="py",
                    report_path=str(rp2)),
    SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
r2 = app2.invoke({"task": TASK, "step_index": 0, "round_in_step": 0,
                  "max_rounds_per_step": 8, "findings": [], "issues": [],
                  "visited": [], "session_id": 1},
                 {"configurable": {"thread_id": "locked1"}})
check("锁死后只 observe 一次", dev2.observes == 1, f"observes={dev2.observes}")
check("锁死后 BLOCKED", r2.get("status") == "BLOCKED", str(r2.get("status")))
check("锁死后零次 act", dev2.acts == 0)

print("\n" + "=" * 46)
print("  全部通过" if ok else "  存在失败项")
print("=" * 46)
sys.exit(0 if ok else 1)
