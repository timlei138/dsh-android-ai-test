"""验证「act 后不再 observe」的节奏（不碰设备）。

SKILL 的核心纪律：observe 次数应明显少于 act 次数。
这个测试用假设备模拟"点两下就到达目标页"，检查：
  1. 总 observe 次数 <= 1（只有会话第一步）
  2. act 次数 == 2
  3. act 返回的 next 被直接用于下一次决策（画面不靠 observe 刷新）
"""

from __future__ import annotations

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


class StepDev:
    """首页 -> 点「更多」-> 点「课程表」-> 到达目标页。

    act 的返回里带 next（模拟真实 act.py 的行为），
    因此图不该再调 observe 取画面。
    """

    def __init__(self):
        self.observes = 0
        self.acts = 0
        self.finished = None
        self.findings = []

    def observe_raw(self):
        self.observes += 1
        return {
            "activity": ".HomeActivity", "package": "com.demo",
            "next": [{"r": "btn_more", "t": "更多", "c": [100, 50]}],
            "event_ids": ["e_obs"],
        }

    def act(self, x, y, why, expect=None, **kw):
        self.acts += 1
        cls = type("R", (), {})

        r = cls()
        r.executed = True
        r.error = None
        r.checkpoint = None
        if self.acts == 1:
            # 点「更多」-> 出现菜单
            r.activity = ".HomeActivity"
            r.next = [{"r": None, "t": "课程表", "c": [200, 300]}]
            r.event_ids = ["e_act1"]
        else:
            # 点「课程表」-> 到达目标页
            r.activity = ".timetable.display.TimetableActivity"
            r.next = [{"r": "btnImportFromGallery", "t": "从图库导入", "c": [300, 400]}]
            r.event_ids = ["e_act2"]
        return r

    def finding(self, *a, **k):
        self.findings.append(a)
        return {"ok": True}

    def finding_no_evidence(self, status, expect, actual):
        """session.py 的 --event-id 是 append 且默认 []，省略即合法。"""
        return self.finding(status, "", expect, actual)
    def finish(self, status, summary):
        self.finished = {"status": status, "summary": summary}
        return {"ok": True}


TASK = {
    "case_id": "loop", "title": "节奏用例", "package": "com.demo",
    "steps": [
        # s1 的落点用文字判据（菜单项「课程表」出现即算到了）
        {"id": "s1", "desc": "点「更多」", "locate": "rid=btn_more",
         "landing": "t=课程表"},
        {"id": "s2", "desc": "点「课程表」", "locate": "t=课程表",
         "landing": "activity=TimetableActivity"},
    ],
    "asserts": [{"id": "a1", "desc": "到达课程表页"}],
    "knowledge_files": [],
}


class Scripted:
    """按脚本回答 decide：点 next[0]；landing 命中就 done。"""

    def __init__(self):
        self.decides = 0

    def __call__(self, payload):
        if payload["kind"] != "decide":
            ev = payload.get("evidence_event_ids") or [""]
            return {"passed": True, "actual": f"activity={payload.get('activity')}",
                    "event_id": ev[0] if ev else ""}
        self.decides += 1
        landing = (payload.get("step_landing") or "").strip()
        act_now = payload.get("activity") or ""
        # 用与图相同的落点判定器，保证语义一致
        hit, _ = ca.match_landing(landing, activity=act_now,
                                  clickable=payload.get("clickable"))
        if hit:
            return {"step_done": True, "why": "落点命中"}
        cl = payload.get("clickable") or []
        if not cl:
            return {"step_done": True, "why": "无目标"}
        c = cl[0]["c"]
        # 优先按 locate 挑，挑不到就取第一个
        locate = (payload.get("step_locate") or "")
        want = locate.split("=", 1)[-1].strip() if "=" in locate else ""
        target = next((n for n in cl if want and (n.get("r") or "") == want), cl[0])
        c = target["c"]
        return {"step_done": False, "next_x": c[0], "next_y": c[1],
                "why": "推进", "expect": landing or None}


print("=== 节奏：act 后不 observe ===")
dev = StepDev()
rp = Path(__file__).resolve().parents[1] / "tmp" / "_loop_test.json"
app = ca.build_child_graph(
    ca.ChildContext(device=dev, skill_root=".", python="py", report_path=str(rp)),
    SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
cfg = {"configurable": {"thread_id": "loop1"}}

r = app.invoke({"task": TASK, "step_index": 0, "round_in_step": 0,
                "max_rounds_per_step": 8, "findings": [], "issues": [],
                "visited": [], "session_id": 1}, cfg)
script = Scripted()
loops = 0
while r.get("__interrupt__") and loops < 40:
    loops += 1
    r = app.invoke(Command(resume=script(r["__interrupt__"][0].value)), cfg)

check("到达目标页", r.get("last_activity") == ".timetable.display.TimetableActivity",
      str(r.get("last_activity")))
check("act 执行 2 次", dev.acts == 2, f"acts={dev.acts}")
check("observe 只 1 次（仅会话第一步）", dev.observes == 1, f"observes={dev.observes}")
check("observe < act（SKILL 自查项）", dev.observes < dev.acts,
      f"observe={dev.observes} act={dev.acts}")
check("结论 PASS", dev.finished and dev.finished["status"] == "PASS",
      str(dev.finished))

print("\n" + "=" * 46)
print("  全部通过" if ok else "  存在失败项")
print("=" * 46)
sys.exit(0 if ok else 1)
