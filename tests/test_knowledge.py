"""测 need_knowledge 闭环 —— 这是 168 实跑里 knowledge: 0 的那个分支。

要证明的链路：
    decide 要知识 -> knowledge 查 -> **回到 decide** 带着命中重新判断 -> act

早先的实现是 knowledge -> act/check，模型从没看到自己要求检索的内容。
这里用一个假应答器模拟"先要知识、拿到后再给坐标"，验证：
  1. knowledge 节点被访问（分支真的走通）
  2. 模型第二轮**确实收到了**命中内容
  3. 不会无限检索（need_knowledge 被清掉）

设备用 tools_bridge.MockDevice（有真实场景图），不自己造假的。
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import android_agent.agent.nodes as ca
from android_agent.device.bridge import MockDevice

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


from android_agent.cases.paths import skill_root  # noqa: E402
SKILL = skill_root()
# 挑一份真的含"课程表"的知识卡，否则检索必然无命中（测不出闭环）。
# ⚠️ 排除 _template.md：它是写卡规范模板，不是真卡片，用它测等于没测。
_know_dir = Path(SKILL) / "knowledge"
_cands = [p for p in sorted(_know_dir.glob("*.md"))
          if p.name != "_template.md"
          and "课程表" in p.read_text(encoding="utf-8", errors="replace")]
KNOW = [f"knowledge/{p.name}" for p in _cands[:1]]
if not KNOW:
    raise SystemExit("找不到含「课程表」的知识卡，测试前提不成立")
print(f"知识卡: {KNOW}")


class FakeCtx(ca.ChildContext):
    """最小 ctx：只要 run.py 不走真机，就不需要真 python/skill。"""

    def __init__(self, dev):
        self.device = dev
        self.skill_root = SKILL
        self.python = sys.executable
        self.report_path = str(Path(__file__).resolve().parents[1] / "tmp" / "_kn_report.json")


class RawMock:
    """给 MockDevice 补 observe_raw（start_check/observe 节点要它）。

    不重造场景图 —— 复用 tools_bridge 里那份，只把 ObserveResult
    转成节点期望的 dict。
    """

    def __init__(self, dev: MockDevice):
        self._d = dev
        self._n = 0
        self.acts = dev.acts
        self.findings = dev.findings

    def observe_raw(self):
        self._n += 1
        o = self._d.observe()
        return {"activity": o.activity,
                # ObserveResult.nodes 用短键 i/t/r/c，节点读的是 next
                "next": o.nodes,
                "event_ids": [f"evt-obs-{self._n}"],
                "evidence": {"png": ""}}

    def act(self, *a, **kw):
        return self._d.act(*a, **kw)

    def finding(self, *a, **kw):
        return self._d.finding(*a, **kw)

    def finding_no_evidence(self, status, expect, actual):
        """session.py 的 --event-id 是 append 且默认 []，省略即合法。"""
        return self.finding(status, "", expect, actual)
    def finish(self, *a, **kw):
        return self._d.finish(*a, **kw)

    def shell(self, command, timeout=60):
        return {"ok": True, "stdout": "", "returncode": 0}


def build_task(knowledge_files=None):
    """图内 task 是 dict（节点用 .get 读）。

    ⚠️ knowledge_files 默认给**空**：本测试要验的是"模型按需检索"这条链路
    （decide 要知识 -> knowledge 查 -> 回 decide）。而第一步现在会
    **自动注入** knowledge_files 的全文，一旦给了文件，模型第一轮就拿到了
    知识，再也不会 need_knowledge —— 分支走不到，测试就成了空转。
    想验自动注入，用 test_knowledge_auto.py。
    """
    return ca.SubagentTask.model_validate({
        "case_id": "kn_loop",
        "title": "知识检索闭环",
        "package": "com.zui.calendar",
        "start_assert": "activity=com.zui.calendar/.MainActivity",
        "knowledge_files": knowledge_files or [],
        "steps": [{"id": "s1", "desc": "打开课程表",
                   "landing": "activity=TimetableActivity"}],        "asserts": [{"id": "a1", "desc": "看到课程表空态", "check": "出现空态提示"}],
    }).model_dump()


seen: list[dict] = []


_asked_once = {"done": False}


def fake_answer(payload):
    """第一轮要知识卡；之后按界面推进（菜单 -> 课程表）。

    场景是两步：点 btnMenu 展开菜单（仍在本 activity），再点 menuTimetable
    才到 TimetableActivity。所以这里要按可点项里有什么来决定点哪，
    否则会一直点同一个坐标、卡在首页（这正是真机上"死循环点同一坐标"的形态）。
    """
    seen.append(payload)
    if payload.get("kind") != "decide":
        return {"passed": True, "actual": "课程表空态出现", "event_id": "evt-0002"}

    if not _asked_once["done"]:
        # 注意：**不看** payload 里有没有 knowledge。
        # 自动注入（第一步给全文）和按需检索是两条独立的路：
        # 就算已经给了整卡，模型也可能想查某个具体关键词。
        # 早先这里加了 `not payload.get("knowledge")` 的前置条件，
        # 自动注入一上线，这个分支就再也走不到了。
        _asked_once["done"] = True
        return {"step_done": False, "need_knowledge": "课程表",
                "why": "先查路径图"}

    # 从当前可点项里挑推进目标（和真模型的约束一致：坐标必须来自列表）
    items = payload.get("clickable") or []
    coords = {item.get("r"): item.get("c") for item in items}
    if "menuTimetable" in coords:
        return {"step_done": False, "next_x": coords["menuTimetable"][0],
                "next_y": coords["menuTimetable"][1],
                "why": "点课程表", "expect": "activity=TimetableActivity"}
    if "btnMenu" in coords:
        return {"step_done": False, "next_x": coords["btnMenu"][0],
                "next_y": coords["btnMenu"][1],
                "why": "点菜单", "expect": "activity=MainActivity"}
    return {"step_done": True, "why": "没有可点项，判定已到位"}


def main() -> None:
    dev = MockDevice()
    ctx = FakeCtx(RawMock(dev))

    visited: list[str] = []
    orig = ca.knowledge_node

    def spy(state):
        visited.append("knowledge")
        return orig(state)

    ca.knowledge_node = spy
    graph = ca.build_child_graph(ctx, InMemorySaver())
    graph.nodes["knowledge"].func = spy  # 替换已注册的节点函数

    ca.set_context(ctx)
    cfg = {"configurable": {"thread_id": "kn-test"}}

    print("\n=== 跑图 ===")
    state = {"task": build_task(KNOW), "step_index": 0, "round_in_step": 0,
             "max_rounds_per_step": 4, "findings": [], "issues": [],
             "visited": [], "pc_done": True, "start_fix_rounds": 0}
    cur = graph.invoke(state, cfg, durability="sync")

    loops = 0
    while "__interrupt__" in cur and loops < 20:
        loops += 1
        intr = cur["__interrupt__"][0]
        payload = intr.value if hasattr(intr, "value") else intr
        cur = graph.invoke(Command(resume=fake_answer(payload)), cfg,
                           durability="sync")

    print(f"  中断轮次: {loops}")

    print("\n=== 1. knowledge 分支被走通 ===")
    check("knowledge 节点被访问", len(visited) >= 1, f"{len(visited)} 次")

    print("\n=== 2. 模型第二轮确实收到了命中 ===")
    decides = [p for p in seen if p.get("kind") == "decide"]
    with_know = [p for p in decides if (p.get("knowledge") or "").strip()]
    check("存在带知识的 decide 轮次", len(with_know) >= 1,
          f"{len(with_know)}/{len(decides)} 轮带知识")
    # 区分两种知识来源：
    #   自动注入 = 整卡全文（第一步，无出处标记）
    #   按需检索 = 命中行（带 [文件:行号]）
    # 本测试验的是**后者**（检索闭环），所以要在所有轮次里找回溯标记，
    # 不能只看第一轮 —— 第一轮很可能是自动注入的整卡。
    searched = [p["knowledge"] for p in with_know if "[" in p["knowledge"]]
    check("检索结果带出处标记", bool(searched),
          (searched[0][:70] if searched else
           f"没有检索命中；各轮来源={['注入' if p['knowledge'].startswith('###') else '检索' for p in with_know]}"))

    print("\n=== 3. 不会无限检索 ===")
    check("knowledge 访问次数受控", len(visited) <= 3, f"{len(visited)} 次")

    print("\n=== 4. 最终结论 ===")
    check("跑到终态（无中断）", "__interrupt__" not in cur)
    print(f"  status={cur.get('status')}  steps={cur.get('step_index')}")
    check("确实点了菜单", any(a.get("rid") == "btnMenu" for a in dev.acts),
          f"acts={dev.acts}")

    print("\n" + "=" * 46)
    if FAIL:
        print(f"  {len(FAIL)} 项失败: {FAIL}")
        sys.exit(1)
    print(f"  全部通过（{len(PASS)} 项）")
    print("=" * 46)


if __name__ == "__main__":
    main()
