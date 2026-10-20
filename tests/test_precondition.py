"""前提条件与恢复的单测（不碰设备）。

验证你定的两条规则：
  1. data 类（pm clear 之类）：可直接执行，清了没问题
  2. env 类（关 WiFi 之类）：**执行后必须恢复原状**，否则干扰下一个 case
     -> env 类没写 undo 时，模型层就该直接拒绝构造
     -> 恢复必须"无论用例成败都执行"（放在 finish 里）
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


class Dev:
    """记录所有 shell 命令；可指定崩溃以验证"失败也要恢复"。

    ⚠️ observe 必须反映**动作后的真实状态**：早先无论做过什么动作都返回
    .Start，导致落点永远不命中、测试自己死循环（不是框架的问题，
    是假设备不真实）。
    """

    def __init__(self, crash=False):
        self.shells = []
        self.crash = crash
        self.acts = 0
        self.observes = 0
        self.finished = None
        self.i = 0
        self.activity = ".Start"

    def observe_raw(self):
        self.observes += 1
        if self.crash:
            return {"activity": ".Home", "terminate": True,
                    "terminate_reason": "启动即崩", "next": [], "event_ids": ["e0"]}
        nxt = ([{"r": "b1", "t": "去", "c": [1, 1]}]
               if self.activity == ".Start" else [])
        return {"activity": self.activity, "next": nxt, "event_ids": ["e0"]}

    def act(self, x, y, why, expect=None, **kw):
        self.acts += 1
        self.i += 1
        self.activity = ".Done"        # 动作真的改变了状态
        r = type("R", (), {})()
        r.executed, r.error, r.checkpoint = True, None, None
        r.activity = self.activity
        r.next = []
        r.event_ids = ["e1"]
        return r

    def shell(self, command, timeout=60):
        self.shells.append(command)
        return ""

    def finding(self, *a, **k):
        return {"ok": True}

    def finding_no_evidence(self, status, expect, actual):
        """session.py 的 --event-id 是 append 且默认 []，省略即合法。"""
        return self.finding(status, "", expect, actual)
    def finish(self, status, summary):
        self.finished = {"status": status, "summary": summary}
        return {"ok": True}


TASK = {
    "case_id": "pc", "title": "前提用例", "package": "com.demo",
    "start_assert": "activity=Start",
    "preconditions": [
        {"kind": "data", "desc": "清数据", "do": ["pm clear com.demo"], "undo": []},
        {"kind": "env", "desc": "关 WiFi 测离线",
         "do": ["svc wifi disable"], "undo": ["svc wifi enable"]},
    ],
    "steps": [{"id": "s1", "desc": "点一下", "locate": "rid=b1",
               "landing": "activity=Done"}],
    "asserts": [{"id": "a1", "desc": "到达"}],
    "knowledge_files": [],
}


def drive(app, cfg, dev, crash=False):
    r = app.invoke({"task": TASK, "step_index": 0, "round_in_step": 0,
                    "max_rounds_per_step": 8, "findings": [], "issues": [],
                    "visited": [], "session_id": 1, "start_fix_rounds": 0}, cfg)
    loops = 0
    while r.get("__interrupt__") and loops < 20:
        loops += 1
        p = r["__interrupt__"][0].value
        if p["kind"] != "decide":
            ev = p.get("evidence_event_ids") or [""]
            ans = {"passed": True, "actual": "ok", "event_id": ev[0] if ev else ""}
        else:
            hit, _ = ca.match_landing(p.get("step_landing") or "",
                                      activity=p.get("activity") or "",
                                      clickable=p.get("clickable"))
            if hit:
                ans = {"step_done": True, "why": "命中"}
            else:
                cl = p.get("clickable") or []
                ans = ({"step_done": True, "why": "无目标"} if not cl else
                       {"step_done": False, "next_x": cl[0]["c"][0],
                        "next_y": cl[0]["c"][1], "why": "go",
                        "expect": p.get("step_landing")})
        r = app.invoke(Command(resume=ans), cfg)
    return r


print("=== env 类缺 undo 必须被拒（模型层拦截）===")
try:
    ca.Precondition(kind="env", desc="关WiFi", do=["svc wifi disable"])
    check("env 缺 undo 被拒", False, "竟然构造成功了")
except Exception as exc:
    check("env 缺 undo 被拒", True, str(exc).splitlines()[-1][:50])

print("\n=== 前提执行 + 收尾恢复 ===")
dev = Dev()
rp = Path(__file__).resolve().parents[1] / "tmp" / "_pc_test.json"
app = ca.build_child_graph(
    ca.ChildContext(device=dev, skill_root=".", python="py", report_path=str(rp)),
    SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
r = drive(app, {"configurable": {"thread_id": "pc1"}}, dev)

check("执行了 data 类清理", "pm clear com.demo" in dev.shells, str(dev.shells))
check("执行了 env 类的 do", "svc wifi disable" in dev.shells, str(dev.shells))
check("执行了 env 类的 undo（恢复）", "svc wifi enable" in dev.shells, str(dev.shells))
check("undo 在最后执行（收尾时）",
      dev.shells and dev.shells[-1] == "svc wifi enable", str(dev.shells))

import json  # noqa: E402
rep = json.loads(rp.read_text(encoding="utf-8"))
check("报告记录已执行前提", any("pm clear" in p for p in rep["preconditions_done"]),
      str(rep["preconditions_done"]))
check("报告记录恢复动作", "svc wifi enable" in rep["restored"], str(rep["restored"]))

print("\n=== 用例失败/崩溃时也必须恢复 ===")
dev2 = Dev(crash=True)
rp2 = Path(__file__).resolve().parents[1] / "tmp" / "_pc_test2.json"
app2 = ca.build_child_graph(
    ca.ChildContext(device=dev2, skill_root=".", python="py", report_path=str(rp2)),
    SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False)))
r2 = drive(app2, {"configurable": {"thread_id": "pc2"}}, dev2)

check("崩溃时仍执行了 undo", "svc wifi enable" in dev2.shells, str(dev2.shells))
rep2 = json.loads(rp2.read_text(encoding="utf-8"))
check("结论为 BLOCKED", rep2["status"] == "BLOCKED", rep2["status"])
check("恢复已记录", "svc wifi enable" in rep2["restored"], str(rep2["restored"]))

print("\n" + "=" * 46)
print("  全部通过" if ok else "  存在失败项")
print("=" * 46)
sys.exit(0 if ok else 1)
