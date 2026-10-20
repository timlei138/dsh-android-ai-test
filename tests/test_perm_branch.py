"""验证"拒绝分支"能穿过整张图到达工具层。

用户要的是 **Android 运行时权限**（不自动点就消失的那种），
并且要记录点了哪几个。SKILL 的规矩：
  · 默认 grant，不声明
  · 测拒绝分支才声明 deny
  · 双分支先拒后允；第二轮必须先 revoke，并清掉 deny

真机上已单独验证过工具层接线（action=deny source=declared，
clicks 全是「拒绝」而非「拒绝并不再询问」）。
这里验证**图这一侧**：用例声明 perm_action 后，
act 节点真的把它传给了设备层（而不是悄悄丢掉）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import android_agent.agent.runner as sa

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


class _Res:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.error = kw.get("error")


class SpyDevice:
    """记录 act 收到的权限意图。"""

    def __init__(self):
        self.calls = []
        self.acts = []
        self.findings = []
        self.finished = None

    def connected(self):
        return True, "spy"

    def start_session(self, **kw):
        return 1

    def observe_raw(self, **kw):
        return {"next": [{"r": "btnAllow", "c": [10, 20]}],
                "package": "com.test", "activity": "A", "event_ids": ["e1"]}

    def act(self, x, y, why="", expect=None, perm_action=None, perm=None, **kw):
        self.calls.append({"x": x, "y": y, "perm_action": perm_action,
                           "perm": perm})
        self.acts.append({"x": x, "y": y})
        return _Res(next=[{"r": "t", "c": [1, 2]}], activity="B",
                    checkpoint=None, executed=True, event_ids=["e2"],
                    permission=None)

    def finding(self, status, event_id, expect, actual):
        self.findings.append({"status": status})
        return {"ok": True}

    def finding_no_evidence(self, status, expect, actual):
        return self.finding(status, "", expect, actual)

    def finish(self, *a, **k):
        return {"ok": True}

    def shell(self, *a, **k):
        return ""

    def perm_intent(self, action, perm=""):
        return {"ok": True}

    def perm_intent_clear(self):
        return {"ok": True}

    def state_perm(self, action, package, perm):
        return {"ok": True}


def run_with(perm_action, perm_name=None):
    dev = SpyDevice()
    rounds = {"i": 0}

    def answer(payload):
        if payload.get("kind") == "decide":
            rounds["i"] += 1
            if rounds["i"] == 1:
                return {"step_done": False, "next_x": 10, "next_y": 20,
                        "why": "触发权限弹窗"}
            return {"step_done": True}
        return {"passed": True, "actual": "到了", "event_id": ""}

    spec = sa.CaseSpec(
        case_id="branch", title="权限分支", package="com.test",
        steps=[{"id": "s1", "desc": "到目标", "landing": "activity=B"}],
        asserts=[{"id": "a1", "desc": "到了", "check": "activity=B"}],
        perm_action=perm_action, perm_name=perm_name,
    )
    sa.run_case(spec, answer=answer, serial=None, diag=True, max_loops=8,
                workdir=Path(__file__).resolve().parents[1] / "tmp" / "_tmp_branch", device=dev, verbose=False)
    return dev.calls


def main():
    print("=== 1. 声明 deny 时，意图到达工具层 ===")
    calls = run_with("deny", "notifications")
    clicked = [c for c in calls if c["x"] == 10]
    check("确实发生了一次 act", len(clicked) >= 1, f"{len(calls)} 次调用")
    if clicked:
        c = clicked[0]
        check("perm_action=deny 传到了设备层", c["perm_action"] == "deny",
              f"实际 {c['perm_action']!r}")
        check("perm 短名也传了", c["perm"] == "notifications", f"实际 {c['perm']!r}")

    print("\n=== 2. 不声明时保持 None（= 工具层默认 grant）===")
    calls = run_with(None)
    clicked = [c for c in calls if c["x"] == 10]
    if clicked:
        check("perm_action 为 None（不干扰默认同意）",
              clicked[0]["perm_action"] is None, f"实际 {clicked[0]['perm_action']!r}")

    print("\n=== 3. CaseSpec 能序列化这两个字段 ===")
    s = sa.CaseSpec(case_id="x", title="t", package="p", steps=[], asserts=[],
                    perm_action="deny", perm_name="camera")
    d = s.model_dump()
    check("perm_action 在 dump 里", d.get("perm_action") == "deny", str(d.get("perm_action")))
    check("perm_name 在 dump 里", d.get("perm_name") == "camera", str(d.get("perm_name")))

    print("\n" + "=" * 46)
    print(f"  全部通过（{len(PASS)} 项）" if not FAIL
          else f"  {len(FAIL)} 项失败: {FAIL}")
    print("=" * 46)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
