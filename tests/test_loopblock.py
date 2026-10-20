"""验证死循环硬阻断真的生效（不是只告警）。

背景：真机实测模型连点同一张照片 9 次、白跑 80 秒。早先只打告警 +
把 history 喂回去，属于"请求"模型改 —— 它照样连点。
用户要求：SKILL 里靠模型自觉的纪律，要用代码约束起来。

这里用一个"故意耍赖"的假模型：每次都坚持点同一个坐标。
如果代码约束有效，它应该被作废掉指定次数，而不是一路点下去。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.agent.runner as sa
from android_agent.device.bridge import MockDevice

ok_all = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_all
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        ok_all = False


print("=" * 60)
print("  死循环硬阻断验证")
print("=" * 60)

# ── 假设备：点多少次界面都不变，专门钓死循环 ──────────────────
class _Res:
    """act 的返回对象（真机按属性读：res.next / res.event_ids / res.activity）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.error = kw.get("error")


class RawMock:
    """MockDevice 的适配层（短键 i/t/r/c -> next/package）。"""

    def __init__(self) -> None:
        self._m = MockDevice()
        self.clicks: list[tuple[int, int]] = []

    def start(self, package: str, **kw) -> dict:
        return {"session_id": 1, "package": package}

    def start_session(self, title="", user_input="", package="", diag=True):
        """run_case 通过它开会话（真机走 session.py start）。"""
        return 1

    def connected(self):
        return True, "注入的假设备"

    def observe_raw(self, **kw):
        r = self._m.observe()
        return {"next": r.nodes, "package": "com.test", "activity": "A"}

    def act(self, x, y, why="", expect=None, **kw):
        self.clicks.append((int(x), int(y)))
        # 关键：无论点几次，界面永远不变、永远还有 menuTimetable
        return _Res(next=[{"r": "menuTimetable", "c": [500, 600]}],
                    activity="A", checkpoint=None, executed=True,
                    event_ids=["evt-loop"])

    def finding_no_evidence(self, *a, **k):
        return {"ok": True}

    def finding(self, *a, **k):
        return {"ok": True}

    def finish(self, *a, **k):
        return {"ok": True}

    def shell(self, *a, **k):
        return ""


dev = RawMock()

# ── 耍赖模型：永远点 (500,600) ────────────────────────────────
stubborn_calls = {"n": 0}


def stubborn(payload: dict) -> dict:
    kind = payload.get("kind")
    if kind == "decide":
        stubborn_calls["n"] += 1
        return {"step_done": False, "next_x": 500, "next_y": 600,
                "why": "我就点这个（耍赖）"}
    if kind == "assert":
        return {"passed": True, "actual": "ok", "event_id": "",
                "confidence": "high"}
    return {}


spec = sa.CaseSpec(
    case_id="loopblock", title="死循环硬阻断", package="com.test",
    # ★ 落点刻意设成一个**假设备永远不会到达**的界面。
    #
    # 早先写的是 landing="activity=A"，而假设备初始就在 A ——
    # 于是"落点已满足"立刻判本步完成，一步没走、0 次决策，
    # 却报 PASS。那种用例测不到死循环保护，只测到了"起点即终点"。
    # 要钓死循环，就必须让这一步**永远做不完**。
    steps=[{"id": "s1", "desc": "走到目标页", "landing": "activity=NEVER"}],
    asserts=[{"id": "a1", "desc": "到了", "check": "activity=NEVER"}],
)

print("\n[场景] 模型每次都坚持点同一坐标 (500,600)")
report = sa.run_case(spec, answer=stubborn, serial=None, diag=True,
                     max_loops=14, workdir=Path(__file__).resolve().parents[1] / "tmp" / "_tmp_loopblock",
                     device=dev)

clicks = dev.clicks
print(f"\n  实际点击次数：{len(clicks)}  模型决策次数：{stubborn_calls['n']}")
print(f"  结论：{report.status}")

# 模型一直在点 (500,600)，所以每次点击都是同一坐标。
# 硬阻断应该让"真正被执行的点击"远少于"模型想点的次数"。
check("模型确实反复想点同一坐标（前提成立）",
      stubborn_calls["n"] >= 4, f"决策 {stubborn_calls['n']} 次")
check("同一坐标没有被无限制执行下去",
      len(clicks) < stubborn_calls["n"],
      f"执行 {len(clicks)} 次 < 决策 {stubborn_calls['n']} 次")
check("运行有界结束（没死循环）",
      report.status in ("PASS", "FAIL", "BLOCKED", "ERROR"), report.status)

print("\n" + "=" * 60)
print("  全部通过" if ok_all else "  存在失败项")
print("=" * 60)
sys.exit(0 if ok_all else 1)
