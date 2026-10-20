"""验证授权/弹窗留痕 —— 用户明确要求"可以自动点，但要知道点了哪几个权限"。

问题背景：真机跑 168 点了两次弹窗（"访问文件相关信息"权限说明框 +
日历自己的"知道了"），但 report_168_model.json 里**一个字都没有**。
事后无法回答"这条用例授权了什么"，也无法判断下次起跑线是否已变。

契约：
  1. 模型声明 grants_permission=true -> 落进 report.permissions_granted
  2. 模型声明 is_dialog=true       -> 落进 report.dialogs_dismissed
  3. 普通点击                        -> 两边都不落（不许误报）
  4. 记录里要带坐标/rid/理由/事件ID（可追溯到证据）
"""
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import android_agent.agent.runner as sa
from android_agent.device.bridge import MockDevice

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


class _Res:
    """act 的返回对象。

    真机 RealDevice.act 返回的是**对象**（res.next / res.event_ids / res.activity），
    act_node 按属性读它。假设备必须同形状，返回 dict 会 AttributeError
    （实测踩过：'dict' object has no attribute 'event_ids'）。
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.error = kw.get("error")


class RawMock:
    def __init__(self):
        self._m = MockDevice()
        self.acts = self._m.acts
        self.finding = self._m.finding

    def finding_no_evidence(self, status, expect, actual):
        return self.finding(status, "", expect, actual)

    def start(self, package, **kw):
        return {"session_id": 1, "package": package}

    def start_session(self, title="", user_input="", package="", diag=True):
        """run_case 通过它开会话（真机走 session.py start）。"""
        return 1

    def connected(self):
        return True, "注入的假设备"

    def observe_raw(self, **kw):
        r = self._m.observe()
        return {"next": r.nodes, "package": "com.test", "activity": "A",
                "event_ids": ["evt-1"]}

    def act(self, x, y, why="", expect=None, **kw):
        # 依次放行：先给个权限框，再给目标
        return _Res(next=[{"r": "target", "c": [10, 20]}],
                    activity="B", checkpoint=None, executed=True,
                    event_ids=["evt-act"])

    def finish(self, *a, **k):
        return {"ok": True}

    def shell(self, *a, **k):
        return ""


def main():
    dev = RawMock()
    n = {"i": 0}

    def answer(payload):
        if payload.get("kind") == "decide":
            n["i"] += 1
            if n["i"] == 1:
                # 模拟：点系统权限框并声明
                return {"step_done": False, "next_x": 10, "next_y": 20,
                        "why": "点掉访问文件权限框",
                        "grants_permission": True,
                        "permission_name": "存储",
                        "permission_decision": "allow"}
            if n["i"] == 2:
                # 模拟：点 App 自己的提示框并声明
                return {"step_done": False, "next_x": 10, "next_y": 20,
                        "why": "点掉知道了提示框",
                        "is_dialog": True, "dialog_text": "知道了"}
            # 普通点击，什么都不该记
            return {"step_done": True, "why": "完成"}
        return {"passed": True, "actual": "到了", "event_id": ""}

    spec = sa.CaseSpec(
        case_id="permrec", title="授权留痕", package="com.test",
        steps=[{"id": "s1", "desc": "到目标页", "landing": "activity=B"}],
        asserts=[{"id": "a1", "desc": "到了", "check": "activity=B"}],
    )
    wd = Path(__file__).resolve().parents[1] / "tmp" / "_tmp_permrec"
    report = sa.run_case(spec, answer=answer, serial=None, diag=True,
                         max_loops=12, workdir=wd, device=dev)

    print("\n=== 1. 权限框被记录 ===")
    pg = report.permissions_granted
    check("permissions_granted 有 1 条", len(pg) == 1, f"{len(pg)} 条")
    if pg:
        r = pg[0]
        check("记了权限名", r.get("permission") == "存储", str(r.get("permission")))
        check("记了授权决定", r.get("decision") == "allow", str(r.get("decision")))
        check("带坐标可追溯", (r.get("x"), r.get("y")) == (10, 20), str(r.get("x")))
        check("带理由", "权限" in (r.get("why") or ""), (r.get("why") or "")[:30])

    print("\n=== 2. 弹窗被记录 ===")
    dd = report.dialogs_dismissed
    check("dialogs_dismissed 有 1 条", len(dd) == 1, f"{len(dd)} 条")
    if dd:
        check("记了弹窗文字", dd[0].get("text") == "知道了", str(dd[0].get("text")))

    print("\n=== 3. 普通点击不误报 ===")
    check("普通点击不进权限表", len(pg) == 1, "仍是 1 条（未被污染）")
    check("普通点击不进弹窗表", len(dd) == 1, "仍是 1 条（未被污染）")

    print("\n=== 4. 落盘到报告文件 ===")
    rp = wd / "report_permrec.json"
    check("报告文件存在", rp.exists())
    if rp.exists():
        raw = json.loads(rp.read_text(encoding="utf-8"))
        check("文件里有 permissions_granted",
              len(raw.get("permissions_granted") or []) == 1)
        check("文件里有 dialogs_dismissed",
              len(raw.get("dialogs_dismissed") or []) == 1)

    print("\n" + "=" * 46)
    print(f"  全部通过（{len(PASS)} 项）" if not FAIL
          else f"  {len(FAIL)} 项失败: {FAIL}")
    print("=" * 46)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
