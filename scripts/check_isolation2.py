"""验证：连跑两个 case，第二个会不会看到第一个的任何东西。

## 这个脚本回答什么

子 Agent 的上下文隔离是这个架构的立身之本 ——
主 Agent 拆解、子 Agent 执行，两者上下文**必须**互不污染。
这个脚本连跑 case A 和 case B，检查：
  1. A 的独有标记会不会漏进 B 的报告
  2. case_id 会不会被上一个串掉
  3. 同名 case 重跑，产物会不会累积

## 产物去哪

工作目录是 `tmp/_iso2/`（不是仓库根）。
早先这里是 `仓库根/_iso2` —— 跑一次就在根目录留一坨 .db/.json/.log，
和"临时产物统一进 tmp/"的规范冲突。tmp 里的东西 24 小时后自动清理。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import android_agent.agent.runner as sa

HERE = Path(__file__).resolve().parent.parent  # 仓库根


class SpyDev:
    """记录子 Agent 会看到的一切 —— 用于检查是否混入上个 case 的内容。"""

    def __init__(self, tag, mark):
        self.tag = tag
        self.mark = mark          # 本 case 独有的标记，不该出现在别处
        self.seen_payloads = []
        self.i = 0

    def connected(self):
        return True, f"spy-{self.tag}"

    def start_session(self, **kw):
        return 1 if self.tag == "A" else 2

    def shell(self, cmd, timeout=60):
        return ""

    def observe_raw(self):
        act = ".Start" if self.i == 0 else ".Done"
        nxt = ([{"r": f"btn_{self.tag}", "t": f"目标_{self.tag}", "c": [1, 1]}]
               if self.i == 0 else [])
        return {"activity": act, "next": nxt, "event_ids": [f"e_{self.tag}"]}

    def act(self, x, y, why, expect=None, **kw):
        self.i += 1
        r = type("R", (), {})()
        r.executed, r.error, r.checkpoint = True, None, None
        r.activity, r.next, r.event_ids = ".Done", [], ["e1"]
        return r

    def finding(self, *a, **k):
        return {"ok": True}

    def finding_no_evidence(self, status, expect, actual):
        """session.py 的 --event-id 是 append 且默认 []，省略即合法。"""
        return self.finding(status, "", expect, actual)
    def finish(self, *a, **k):
        return {"ok": True}


def make_spec(case_id, mark):
    return {
        "case_id": case_id, "title": f"隔离-{case_id}", "package": "p",
        "start_assert": "activity=Start",
        "preconditions": [{"kind": "data", "desc": f"标记{mark}",
                           "do": [f"pm clear {mark}"], "undo": []}],
        "steps": [{"id": "s1", "desc": f"点 {mark}", "locate": f"rid=btn_{mark}",
                   "landing": "activity=Done"}],
        "asserts": [{"id": "a1", "desc": f"到 {mark}"}],
        "knowledge_files": [],
    }


print("=== 连跑 case A 与 case B，检查互不串味 ===")
# 产物写 tmp/_iso2/，不写仓库根（见文件头说明）
wd = HERE / "tmp" / "_iso2"
wd.mkdir(parents=True, exist_ok=True)
results = {}
for tag in ("A", "B"):
    dev = SpyDev(tag, tag)
    orig = sa.RealDevice
    sa.RealDevice = lambda *a, **k: dev
    try:
        rep = sa.run_case(sa.CaseSpec(**make_spec(tag, tag)),
                          workdir=wd, verbose=False)
    finally:
        sa.RealDevice = orig
    results[tag] = rep
    print(f"  {tag}: status={rep.status} steps={rep.steps_done} "
          f"issues={len(rep.issues)} summary={rep.summary[:40]}")

print("\n=== 交叉污染检查 ===")
bad = []
for tag, rep in results.items():
    blob = rep.model_dump_json()
    other = "B" if tag == "A" else "A"
    # 上个/下个 case 的独有标记不该出现在本 case 的报告里
    if f"_{other}" in blob:
        bad.append(f"{tag} 报告里出现了 {other} 的痕迹")
    if rep.case_id != tag:
        bad.append(f"{tag} 的 case_id 变成了 {rep.case_id}")

for f in sorted(wd.glob("child_*.db")):
    print(f"  checkpoint 文件：{f.name}")

print("\n  交叉污染：" + ("无 ✅" if not bad else "发现 -> " + "; ".join(bad)))

print("\n=== 同名 case 重跑（最容易残留的场景）===")
dev1 = SpyDev("A", "A")
orig = sa.RealDevice
sa.RealDevice = lambda *a, **k: dev1
try:
    r1 = sa.run_case(sa.CaseSpec(**make_spec("same", "A")), workdir=wd, verbose=False)
finally:
    sa.RealDevice = orig
print(f"  第1次：steps={r1.steps_done} findings={len(r1.findings)}")

dev2 = SpyDev("A", "A")
sa.RealDevice = lambda *a, **k: dev2
try:
    r2 = sa.run_case(sa.CaseSpec(**make_spec("same", "A")), workdir=wd, verbose=False)
finally:
    sa.RealDevice = orig
print(f"  第2次：steps={r2.steps_done} findings={len(r2.findings)}")
print("  重跑不累积：" + ("✅" if r2.steps_done == r1.steps_done == 1 else "❌ 有累积"))
