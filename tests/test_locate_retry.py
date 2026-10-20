"""「换目标找」的有界重试。

用户要求（原话）：
    「rid 太严了。有些没有。我理解可以找以下。
      但次数要有个限制。不能无限次找。同时记录里要提示出来。」

用例 YAML 的规矩是「目标 rid 找不到 → 立即 BLOCKED」，太严 ——
很多控件本来就没 rid（iv_more 没 text/desc，菜单项连 rid 都没有）。
但完全不限也不行：会变成"模型随便试到天荒地老"。

所以规矩是三条：
    1) 允许换目标找
    2) 限次（默认 3）
    3) 每次都要留痕

这个测试盯的就是这三条。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.agent.nodes import step_check_node, target_present  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


STEP = {"id": "s1", "desc": "点更多", "locate": "rid=iv_more",
        "landing": "activity=TimetableActivity", "on_fail": "BLOCKED"}


def state(**over):
    s = {
        "task": {"steps": [STEP], "package": "com.zui.calendar"},
        "step_index": 0,
        "round_in_step": 1,
        "max_rounds_per_step": 8,
        "locate_attempts": 0,
        "max_locate_attempts": 3,
        "last_activity": ".AllInOneActivity",
        "action": {"step_done": False, "why": "屏幕上没有 iv_more"},
        "obs_clickable": [],
        "obs_nodes_text": "",
    }
    s.update(over)
    return s


print("=== 1) target_present 判得准吗 ===")
check("没声明 locate -> None（不参与决策）",
      target_present("", clickable=[]) is None)
check("rid 在屏上 -> True",
      target_present("rid=iv_more", clickable=[{"r": "iv_more"}]) is True)
check("rid 不在 -> False",
      target_present("rid=iv_more", clickable=[{"r": "other"}]) is False)
check("text 判据也能找到 -> True",
      target_present("text=课程表", clickable=[{"r": "x", "t": "课程表"}]) is True)
check("不可点的文案也能找到（靠 nodes_text 兜底）",
      target_present("text=课程表", clickable=[], nodes_text="课程表") is True)

print("\n=== 2) 目标不在 -> 记一次，继续找 ===")
out = step_check_node(state())
check("记了 1 次尝试", out.get("locate_attempts") == 1,
      str(out.get("locate_attempts")))
check("没直接判死（还在重试）", out.get("status") is None,
      str(out.get("status")))
check("有留痕 locate_tried", bool(out.get("locate_tried")))
tri = (out.get("locate_tried") or [{}])[0]
check("留痕含：第几次/上限/目标/当前界面",
      tri.get("attempt") == 1 and tri.get("of") == 3
      and tri.get("locate") == "rid=iv_more"
      and tri.get("activity") == ".AllInOneActivity", str(tri))
check("issues 里明确提示了",
      any("换目标找" in i and "1/3" in i for i in out.get("issues", [])),
      str(out.get("issues")))

print("\n=== 3) 累计到上限 -> 停这一步（不无限找）===")
out = step_check_node(state(locate_attempts=3))
check("第 4 次触发停止", out.get("status") == "BLOCKED",
      str(out.get("status")))
check("说清是查找次数用尽",
      "查找次数用尽" in str(out.get("issues")), str(out.get("issues")))
check("推进到下一步（不卡死整条用例）",
      out.get("step_index") == 1, str(out.get("step_index")))
check("findings 标了 locate_exhausted",
      (out.get("findings") or [{}])[0].get("status") == "locate_exhausted",
      str(out.get("findings")))
check("summary 提到 on_fail 策略",
      "on_fail" in str(out.get("summary")), str(out.get("summary")))

print("\n=== 4) 目标在 -> 根本不该消耗额度 ===")
out = step_check_node(state(
    obs_clickable=[{"r": "iv_more", "c": [100, 200]}]))
check("没有记尝试", out.get("locate_attempts") is None,
      str(out.get("locate_attempts")))
check("没有留痕", not out.get("locate_tried"), str(out.get("locate_tried")))

print("\n=== 5) 未声明 locate 的步骤不受影响（老行为不变）===")
s2 = state()
s2["task"] = {"steps": [{"id": "s1", "desc": "x",
                         "landing": "activity=Foo"}],
              "package": "com.zui.calendar"}
out = step_check_node(s2)
check("不记尝试", out.get("locate_attempts") is None)
check("不报错、正常重试", out.get("status") is None)

print("\n=== 6) 换目标找的理由被记下来（区分 UI 变了 vs 模型乱试）===")
out = step_check_node(state(action={"step_done": False,
                                    "why": "想改用文本定位试试"}))
tri = (out.get("locate_tried") or [{}])[0]
check("why 被留痕", tri.get("why") == "想改用文本定位试试", str(tri))
check("issues 里也带上了 why",
      any("想改用文本定位试试" in i for i in out.get("issues", [])),
      str(out.get("issues")))

print(f"\n{'=' * 50}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 50}")
sys.exit(1 if fail else 0)
