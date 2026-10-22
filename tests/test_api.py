"""通用模块 API 的验收测试（不碰设备）。

证明「任何 case 都能用这个模块执行」：
  case A：3 步链路，全绿
  case B：2 步，第 2 步落点不符 -> FAIL（不是 PASS）
  case C：1 步就走不通 -> 不卡死，BLOCKED/FAIL 且给出原因
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import android_agent.agent.nodes as ca  # noqa: E402
import android_agent.agent.runner as sa  # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


class FakeDev:
    """可编排的假设备：按 (activity, next) 脚本逐次返回。"""

    def __init__(self, script):
        self.script = script      # [{"activity":..,"next":[..]}]
        self.i = 0
        self.observes = 0
        self.acts = 0
        self.finished = None
        self.findings = []
        self.shells = []
        self.session_id = 1

    def connected(self):
        return True, "假设备"

    def start_session(self, **kw):
        return 1

    def shell(self, command, timeout=60):
        """前提准备/恢复用。假设备只记录，不真执行。"""
        self.shells.append(command)
        return ""

    def observe_raw(self):
        self.observes += 1
        s = self.script[min(self.i, len(self.script) - 1)]
        return {"activity": s["activity"], "next": s.get("next", []),
                "event_ids": [f"e{self.i}"]}

    def act(self, x, y, why, expect=None, **kw):
        self.acts += 1
        self.i += 1
        s = self.script[min(self.i, len(self.script) - 1)]
        r = type("R", (), {})()
        r.executed, r.error, r.checkpoint = True, None, None
        r.activity = s["activity"]
        r.next = s.get("next", [])
        r.event_ids = [f"e{self.i}"]
        return r

    def finding(self, status, event_id, expect, actual):
        self.findings.append({"status": status, "actual": actual})
        return {"ok": True}

    def finding_no_evidence(self, status, expect, actual):
        """session.py 的 --event-id 是 append 且默认 []，省略即合法。"""
        return self.finding(status, "", expect, actual)
    def finish(self, status, summary):
        self.finished = {"status": status, "summary": summary}
        return {"ok": True}


def run_with(script, spec_dict, answer=None):
    """用假设备跑一遍。

    ⚠️ 早先靠 `sa.RealDevice = lambda ...` 打补丁 —— 但直连层换成
    DirectDevice 之后那个补丁打不着了，测试会**偷偷连上真机**
    （日志里 "设备：已连接：3040x1904 com.zui.calendar" 就是证据），
    然后在一个真实 App 上跑假脚本，结果全错。
    改用 run_case 本来就支持的 `device=` 注入，不依赖内部实现细节。
    """
    dev = FakeDev(script)
    _wd = Path(__file__).resolve().parents[1] / "tmp" / "_tmp_api"
    rep = sa.run_case(sa.CaseSpec(**spec_dict), answer=answer,
                      verbose=False, device=dev,
                      # ★ 生成的 YAML 留在临时目录里 —— 不传的话会写进
                      #   用例库，测试夹具被当成真用例（污染过 A_三步链路）。
                      yaml_out_dir=_wd,
                      workdir=_wd)
    return rep, dev


# 一个"落点命中就 done，否则点第一个可点项"的通用应答器
def make_answer():
    def _ans(p):
        if p["kind"] != "decide":
            ev = p.get("evidence_event_ids") or [""]
            return {"passed": True,
                    "actual": f"activity={p.get('activity')} 且节点符合预期",
                    "event_id": ev[0] if ev else ""}
        hit, _ = ca.match_landing(p.get("step_landing") or "",
                                  activity=p.get("activity") or "",
                                  clickable=p.get("clickable"))
        if hit:
            return {"step_done": True, "why": "落点命中"}
        cl = p.get("clickable") or []
        if not cl:
            return {"step_done": True, "why": "无可点项"}
        c = cl[0]["c"]
        return {"step_done": False, "next_x": c[0], "next_y": c[1],
                "why": "推进", "expect": p.get("step_landing")}
    return _ans


print("=== case A：三步链路全绿 ===")
scriptA = [
    {"activity": ".Home", "next": [{"r": "b1", "t": "更多", "c": [1, 1]}]},
    {"activity": ".Menu", "next": [{"r": "b2", "t": "课程表", "c": [2, 2]}]},
    {"activity": ".Timetable", "next": [{"r": "b3", "t": "导入", "c": [3, 3]}]},
    {"activity": ".Picker", "next": []},
]
specA = {
    "case_id": "A", "title": "三步链路", "package": "com.demo",
    "steps": [
        {"id": "s1", "desc": "点更多", "locate": "rid=b1", "landing": "t=课程表"},
        {"id": "s2", "desc": "点课程表", "locate": "rid=b2", "landing": "activity=Timetable"},
        {"id": "s3", "desc": "点导入", "locate": "rid=b3", "landing": "activity=Picker"},
    ],
    "asserts": [{"id": "a1", "desc": "进到选择器"}],
    "knowledge_files": [],
}
repA, devA = run_with(scriptA, specA, make_answer())
check("A 状态 PASS", repA.status == "PASS", repA.status)
check("A 三步全完成", repA.steps_done == 3, str(repA.steps_done))
check("A coverage_ok", repA.coverage_ok is True)
check("A observe 少于 act", devA.observes < devA.acts,
      f"obs={devA.observes} act={devA.acts}")

print("\n=== case B：末步落点不符 -> FAIL（不得 PASS）===")
scriptB = [
    {"activity": ".Home", "next": [{"r": "b1", "t": "更多", "c": [1, 1]}]},
    {"activity": ".Menu", "next": [{"r": "b2", "t": "课程表", "c": [2, 2]}]},
    # 点下去但停在 .Home，没到 Timetable
    {"activity": ".Home", "next": []},
]
specB = {
    "case_id": "B", "title": "落点不符", "package": "com.demo",
    "steps": [
        {"id": "s1", "desc": "点更多", "locate": "rid=b1", "landing": "t=课程表"},
        {"id": "s2", "desc": "点课程表", "locate": "rid=b2",
         "landing": "activity=TimetableActivity"},
    ],
    "asserts": [{"id": "a1", "desc": "进到课程表"}],
    "knowledge_files": [],
}
repB, devB = run_with(scriptB, specB, make_answer())
check("B 不报 PASS", repB.status != "PASS", repB.status)
check("B coverage_ok=False", repB.coverage_ok is False)
check("B 有未达成记录", len(repB.issues) > 0, str(repB.issues)[:90])

print("\n=== case C：走不通不卡死 ===")
scriptC = [{"activity": ".Home", "next": []}]   # 没有任何可点项
specC = {
    "case_id": "C", "title": "走不通", "package": "com.demo",
    "steps": [{"id": "s1", "desc": "点不存在的按钮", "locate": "rid=nope",
               "landing": "activity=Never"}],
    "asserts": [{"id": "a1", "desc": "永远到不了"}],
    "knowledge_files": [],
}
repC, devC = run_with(scriptC, specC, make_answer())
check("C 正常收尾（未抛异常）", repC is not None)
check("C 不报 PASS", repC.status != "PASS", repC.status)
check("C act 次数受控（不无限重试）", devC.acts <= 10, f"acts={devC.acts}")

print("\n=== 模块 API：CaseSpec 可序列化 ===")
p = Path(__file__).resolve().parents[1] / "tmp" / "_tmp_api" / "specA.json"
specA_obj = sa.CaseSpec(**specA)
specA_obj.to_json(p)
back = sa.CaseSpec.from_json(p)
check("json 往返一致", back.model_dump() == specA_obj.model_dump())
check("公开 API 齐全",
      all(hasattr(sa, n) for n in
          ["run_case", "CaseSpec", "StepSpec", "AssertSpec",
           "match_landing", "SubagentReport"]))

print("\n" + "=" * 46)
print("  全部通过" if ok else "  存在失败项")
print("=" * 46)
sys.exit(0 if ok else 1)
