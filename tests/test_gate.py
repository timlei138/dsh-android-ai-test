"""子 Agent 结论闸门的单元测试（不碰设备）。

重点验证上一轮暴露的 bug：步骤落点没达成时**不得报 PASS**。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import android_agent.agent.nodes as ca  # noqa: E402

ok_all = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok_all
    if not cond:
        ok_all = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


class FakeDev:
    def __init__(self) -> None:
        self.finished = None
        self.findings = []

    def finish(self, status, summary):
        self.finished = {"status": status, "summary": summary}
        return {"ok": True}

    def finding(self, status, event_id, expect, actual):
        self.findings.append({"status": status, "id": event_id})
        return {"ok": True}

    def finding_no_evidence(self, status, expect, actual):
        """session.py 的 --event-id 是 append 且默认 []，省略即合法。"""
        return self.finding(status, "", expect, actual)


TASK = {
    "case_id": "t", "title": "T", "package": "p",
    "steps": [{"id": "s1", "desc": "d1", "landing": "activity=A"},
              {"id": "s2", "desc": "d2", "landing": "activity=B"},
              {"id": "s3", "desc": "d3", "landing": "activity=C"}],
    "asserts": [{"id": "a1", "desc": "e1"}, {"id": "a2", "desc": "e2"},
                {"id": "a3", "desc": "e3"}],
    "knowledge_files": [],
}


def finish_with(steps_findings, assert_findings, issues, status=None):
    dev = FakeDev()
    # 报告写 tmp/，别落到仓库根 —— 这里测的是闸门逻辑，
    # 产物扔在根目录只会让仓库越来越脏（之前就漏了一个 _gate_test.json）。
    _rp = Path(__file__).resolve().parents[1] / "tmp" / "_gate_test.json"
    ca.set_context(ca.ChildContext(device=dev, skill_root=".", python="py",
                                   report_path=str(_rp)))
    st = {"task": TASK, "findings": steps_findings + assert_findings,
          "issues": issues, "session_id": 1}
    if status:
        st["status"] = status
    out = ca.finish_child_node(st)
    return out, dev


print("=== 闸门：步骤落点未达成不得 PASS ===")
# 上一轮真实翻车场景：3 步都声明完成但落点全不符，steps_done=0
out, dev = finish_with(
    steps_findings=[],
    assert_findings=[{"kind": "assert", "id": f"a{i}", "status": "PASS"}
                     for i in (1, 2, 3)],
    issues=["s1 声明完成但落点不符：期望 activity=A，实际 .MainActivity",
            "s2 声明完成但落点不符：期望 activity=B，实际 .MainActivity",
            "s3 声明完成但落点不符：期望 activity=C，实际 .MainActivity"])
check("3 步全没走完 -> BLOCKED（不是 PASS）", out["status"] == "BLOCKED",
      out["status"])
check("给出未完成步数", any("步骤" in i for i in out["_report"]["issues"]),
      str(out["_report"]["issues"]))
check("coverage_ok=False", out["_report"]["coverage_ok"] is False)
check("steps_done=0", out["_report"]["steps_done"] == 0)

print("\n=== 闸门：步骤走完且落点都对 -> PASS ===")
out, dev = finish_with(
    steps_findings=[{"kind": "step", "step": f"s{i}", "status": "ok"}
                    for i in (1, 2, 3)],
    assert_findings=[{"kind": "assert", "id": f"a{i}", "status": "PASS"}
                     for i in (1, 2, 3)],
    issues=[])
check("全通过 -> PASS", out["status"] == "PASS", out["status"])
check("steps_done=3", out["_report"]["steps_done"] == 3)
check("coverage_ok=True", out["_report"]["coverage_ok"] is True)

print("\n=== 闸门：步骤走完但某步落点不符 -> FAIL ===")
out, _ = finish_with(
    steps_findings=[{"kind": "step", "step": f"s{i}", "status": "ok"}
                    for i in (1, 2, 3)],
    assert_findings=[{"kind": "assert", "id": f"a{i}", "status": "PASS"}
                     for i in (1, 2, 3)],
    issues=["s2 声明完成但落点不符：期望 activity=B，实际 .MainActivity"])
check("落点不符 -> FAIL", out["status"] == "FAIL", out["status"])
check("coverage_ok=False", out["_report"]["coverage_ok"] is False)

print("\n=== 闸门：预期没收齐 -> BLOCKED ===")
out, _ = finish_with(
    steps_findings=[{"kind": "step", "step": f"s{i}", "status": "ok"}
                    for i in (1, 2, 3)],
    assert_findings=[{"kind": "assert", "id": "a1", "status": "PASS"}],
    issues=[])
check("只验 1/3 -> BLOCKED", out["status"] == "BLOCKED", out["status"])

print("\n=== 闸门：某条预期 FAIL -> FAIL ===")
out, _ = finish_with(
    steps_findings=[{"kind": "step", "step": f"s{i}", "status": "ok"}
                    for i in (1, 2, 3)],
    assert_findings=[{"kind": "assert", "id": "a1", "status": "PASS"},
                     {"kind": "assert", "id": "a2", "status": "FAIL"},
                     {"kind": "assert", "id": "a3", "status": "PASS"}],
    issues=[])
check("含 FAIL -> FAIL", out["status"] == "FAIL", out["status"])

print("\n=== 步骤计数：kind 必须是 step ===")
check("_steps_done 只认 kind=step",
      ca._steps_done({"findings": [{"kind": "step"}, {"kind": "assert"}]}) == 1)

print("\n" + "=" * 46)
print("  全部通过" if ok_all else "  存在失败项")
print("=" * 46)
sys.exit(0 if ok_all else 1)
