"""回归：交替死循环必须被代码拦住。

真机现场（168，一次被中止的运行）：
    轮51 点 (337,203)  top_button_date_layout
    轮52 点 (1244,1276) left_button
    轮53 点 (337,203)
    轮54 点 (1244,1276)
    ...  一路到轮58，6 分钟全是这两点在来回

原来的硬阻断只判"**连续 3 次同一个点**"：
    repeated = recent[-1] == key and recent[-2] == key
A→B→A→B 交替时，recent[-1] 永远不等于 recent[-2]，
所以一次都没触发 —— 模型就在两个页面之间弹了 58 轮。

这个测试用真实的两点交替序列，验证会被识别为死循环并终止。
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


class PingPongDevice:
    """模拟真机：两个页面来回切，模型永远在两点之间弹。"""

    def __init__(self):
        self.acts = []

    def connected(self):
        return True, "fake"

    def start_session(self, **kw):
        return 1

    def observe_raw(self, **kw):
        return {"next": [{"r": "btnImportFromGallery", "c": [100, 200]}],
                "package": "com.test", "activity": "A", "event_ids": ["e1"]}

    def act(self, x, y, why="", expect=None, perm_action=None, perm=None, **kw):
        self.acts.append((x, y))
        # 两个页面交替出现 —— 正是真机上的样子
        if (x, y) == (337, 203):
            nxt = [{"r": "left_button", "c": [1244, 1276]},
                   {"r": "right_button", "c": [1795, 1276]}]
        else:
            nxt = [{"r": "top_button_date_layout", "c": [337, 203]},
                   {"r": "agenda_item_text_container", "c": [2252, 757]}]
        return _Res(next=nxt, activity="A", checkpoint=None, executed=True,
                    event_ids=["e2"], permission=None, package="com.test",
                    error=None)

    def finding(self, s, e, x, a):
        return {"ok": True}

    def finding_no_evidence(self, s, x, a):
        return {"ok": True}

    def finish(self, *a, **k):
        return {"ok": True}

    def shell(self, *a, **k):
        return ""

    def perm_intent(self, a, p=""):
        return {"ok": True}

    def perm_intent_clear(self):
        return {"ok": True}

    def state_perm(self, a, pkg, p):
        return {"ok": True}


dev = PingPongDevice()
n = {"v": 0}


def answer(payload):
    """永远在两个点之间弹 —— 从不判定完成、从不放弃。"""
    if payload.get("kind") == "decide":
        n["v"] += 1
        if n["v"] % 2 == 1:
            return {"step_done": False, "next_x": 337, "next_y": 203,
                    "why": "点日期栏"}
        return {"step_done": False, "next_x": 1244, "next_y": 1276,
                "why": "点左按钮"}
    return {"passed": False, "actual": "没到", "event_id": "e2"}


spec = sa.CaseSpec(
    case_id="pingpong", title="交替死循环", package="com.test",
    steps=[{"id": "s1", "desc": "到目标", "landing": "activity=TargetActivity"}],
    asserts=[{"id": "a1", "desc": "到了", "check": "activity=TargetActivity"}],
)

report = sa.run_case(spec, answer=answer, serial=None, diag=True, max_loops=60,
                     workdir=Path(__file__).resolve().parents[1] / "tmp" / "_tmp_pingpong", device=dev, verbose=False)

print("=== 1. 交替循环被识别 ===")
cyc = [i for i in report.issues if "死循环" in i or "交替" in i]
check("报了交替死循环", len(cyc) >= 1, str(cyc[:1]))

print("\n=== 2. 没有空转到轮次上限 ===")
# 真机上是 58 轮才发现（等于没发现）。要求远早于 max_loops=60 就终止。
check("点击次数被有效限制", len(dev.acts) <= 20,
      f"实际点了 {len(dev.acts)} 次（上限 60）")

print("\n=== 3. 结论不算 PASS（不能把卡死洗成通过）===")
check("不是 PASS", report.status != "PASS", f"status={report.status}")

print("\n=== 4. 单点连点仍然照旧拦得住 ===")
dev2 = PingPongDevice()
n2 = {"v": 0}


def answer_same(payload):
    if payload.get("kind") == "decide":
        n2["v"] += 1
        return {"step_done": False, "next_x": 500, "next_y": 500, "why": "死点一个地方"}
    return {"passed": False, "actual": "没到", "event_id": "e2"}


report2 = sa.run_case(spec, answer=answer_same, serial=None, diag=True,
                      max_loops=60, workdir=Path(__file__).resolve().parents[1] / "tmp" / "_tmp_pingpong2",
                      device=dev2, verbose=False)
check("同点连点也终止了", len(dev2.acts) <= 20, f"实际 {len(dev2.acts)} 次")
check("同点连点不是 PASS", report2.status != "PASS", f"status={report2.status}")

print("\n" + "=" * 46)
print(f"  全部通过（{len(PASS)} 项）" if not FAIL
      else f"  {len(FAIL)} 项失败: {FAIL}")
print("=" * 46)
raise SystemExit(0 if not FAIL else 1)
