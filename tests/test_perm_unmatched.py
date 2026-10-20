"""回归：权限框"无人可点"时必须交回决策，不能继续瞎点。

用户指出的真实场景：
  第二次申请「访问您设备上的照片」（照片/相机）权限时，
  Android 的弹窗多出 **「拒绝并不再询问」**。
  加上「允许访问所选照片」，两个按钮工具层都**故意不点**：
    · 「拒绝并不再询问」→ 设 don't-ask-again，后续授权分支永远不再弹窗
    · 「允许访问所选照片」→ 部分授权，语义不对
  于是 unmatched=True。

早先的行为：只 issues.append 记一笔，**流程继续往下点**。
而权限框还挡在屏幕上 → 后续点击全落在框上
（真机实测连点 scrollView 12 次不动，白等 80 秒）。

修完后：置 perm_unmatched，decide 收到现场，
模型按用例选一个按钮（或 give_up），而不是继续点别的。
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


class UnmatchedDevice:
    """模拟：设备上有个点不动的权限框（工具层无匹配按钮）。"""

    def __init__(self):
        self.acts = []
        self.findings = []

    def connected(self):
        return True, "fake"

    def start_session(self, **kw):
        return 1

    def observe_raw(self, **kw):
        return {"next": [{"r": "btnImportFromGallery", "c": [100, 200]}],
                "package": "com.test", "activity": "A",
                "event_ids": ["e1"]}

    def act(self, x, y, why="", expect=None, perm_action=None, perm=None, **kw):
        self.acts.append((x, y))
        return _Res(
            next=[{"r": "btnImportFromGallery", "c": [100, 200]}],
            activity="A", checkpoint=None, executed=True,
            event_ids=["e2"],
            # 关键的现场：框在、点了没匹配上
            permission={
                "detected": True, "handled": False, "unmatched": True,
                "buttons": ["拒绝并不再询问", "允许访问所选照片"],
                "prompts": ["日历正在尝试访问您设备上的照片"],
                "activity": ".permission.ui.GrantPermissionsActivity",
                "clicks": [],
            })

    def finding(self, s, e, x, a):
        self.findings.append(s)
        return {"ok": True}

    def finding_no_evidence(self, s, x, a):
        return self.finding(s, "", x, a)

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


dev = UnmatchedDevice()
seen = {"payload": None, "n": 0}


def answer(payload):
    if payload.get("kind") == "decide":
        seen["n"] += 1
        # 第一次点：触发权限框
        if seen["n"] == 1:
            return {"step_done": False, "next_x": 100, "next_y": 200,
                    "why": "点导入入口"}
        # 第二次：模型应该看到 perm_unmatched 现场
        seen["payload"] = payload
        return {"step_done": False, "next_x": 100, "next_y": 200,
                "why": "按现场处置"}
    return {"passed": True, "actual": "ok", "event_id": "e2"}


spec = sa.CaseSpec(
    case_id="unmatched", title="权限框无人可点", package="com.test",
    # ★ 落点必须是这台假设备**永远到不了**的界面。
    #   早先写 landing="activity=A"，而假设备一直在 A —— 于是"落点已满足"
    #   立刻判本步完成，一次都没点，权限框压根没机会出现，
    #   perm_unmatched 自然是 None。测的东西根本没被执行到。
    steps=[{"id": "s1", "desc": "到目标", "landing": "activity=Picker"}],
    asserts=[{"id": "a1", "desc": "到了", "check": "activity=Picker"}],
)
report = sa.run_case(spec, answer=answer, serial=None, diag=True, max_loops=8,
                     workdir=Path(__file__).resolve().parents[1] / "tmp" / "_tmp_unmatched", device=dev, verbose=False)

print("=== 1. 现场被交回模型 ===")
pl = seen["payload"]
check("模型收到了 perm_unmatched", pl is not None and pl.get("perm_unmatched"),
      str(pl and pl.get("perm_unmatched")))
if pl and pl.get("perm_unmatched"):
    pu = pl["perm_unmatched"]
    check("带上了可见按钮",
          "拒绝并不再询问" in str(pu.get("buttons")), str(pu.get("buttons")))
    check("带上了正在申请的权限",
          "照片" in str(pu.get("prompts")), str(pu.get("prompts")))

print("\n=== 2. 问题如实记录 ===")
issues = [i for i in report.issues if "无匹配按钮" in i]
check("记了 unmatched 问题", len(issues) >= 1, str(issues[:1]))

print("\n=== 3. 没有把「拒绝并不再询问」当成点击对象 ===")
# 设备层的 act 只应收到模型给的坐标，不应收到工具层对危险按钮的自动点击
check("工具层没有自作主张点危险按钮",
      all(a == (100, 200) for a in dev.acts), str(dev.acts))

print("\n" + "=" * 46)
print(f"  全部通过（{len(PASS)} 项）" if not FAIL
      else f"  {len(FAIL)} 项失败: {FAIL}")
print("=" * 46)
raise SystemExit(0 if not FAIL else 1)
