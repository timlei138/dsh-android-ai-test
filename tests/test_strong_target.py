"""强证据直点：target 在当前屏能否唯一确定？

★ 这不是回放。设计依据见 docs/执行机制.md 第 3、4 节：

    「YAML 里强有力的判断可以直接执行，不用通过 LLM。
      比如点击某个按钮跳转到了具体的页面，或者点击后出现了某个特有的弹窗，
      或者出现了独一一个 view。强力证据 rid desc text 这种。
      最好用 xpath 这种组合的路径，防止出现两个相同的找错的问题。」

    「YAML 也只是作为一个第一级别的参考文件。
      不同设备或者分辨率的回放有问题。YAML 里严谨存储坐标信息 ——
      这个非常不可靠的东西。」（坐标禁止，故 YAML 里不存坐标）

这个测试盯的是三件事：
    ① 唯一命中 → 代码直接点，**不问模型**
    ② 命中 0 个 / 多于 1 个 → 必须交给模型，**绝不赌**
    ③ 落点预判只在"本步还没动过手"时成立，避免"起点即终点"的假通过
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.agent import nodes as nd
from android_agent.device.direct import DirectDevice, Screen

ok_all = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_all
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        ok_all = False


def make_state(*, clickable, xml_path="", landing=None, target=None,
               round_in_step=0, activity="A"):
    step = {"id": "s1", "desc": "测试步骤"}
    if landing:
        step["landing"] = landing
    if target:
        step["target"] = target
    return {
        "task": {"steps": [step], "package": "com.test"},
        "step_index": 0,
        "round_in_step": round_in_step,
        "last_activity": activity,
        "last_package": "com.test",
        "obs_activity": activity,
        "obs_clickable": clickable,
        "obs_nodes_text": "",
        "obs_xml_path": xml_path,
        "findings": [], "issues": [], "progress": [], "visited": [],
        "last_event_ids": ["evt1"],
    }


print("=" * 66)
print("  1) rid 唯一命中 → 代码直接点，不问模型")
print("=" * 66)
st = make_state(clickable=[{"i": 0, "r": "iv_more", "c": [100, 200]},
                           {"i": 1, "r": "btn_ok", "c": [300, 400]}],
                target="rid=iv_more")
out = nd.strong_target_node(st)
act = out.get("action") or {}
check("拿到动作", bool(act), str(act)[:70])
check("坐标取自命中项", (act.get("next_x"), act.get("next_y")) == (100, 200),
      f"{act.get('next_x')},{act.get('next_y')}")
check("没有落回模型", not out.get("fallback_reason"))
check("记为代码直点",
      any(h.get("by") == "code" for h in (out.get("strong_hits") or [])))
check("路由去 act", nd.route_after_strong({**st, **out}) == "act")

print()
print("=" * 66)
print("  2) ★ rid 命中多个 → 必须交给模型（不能瞎点第一个）")
print("=" * 66)
# 实测场景：设置页有 4 个 rid/desc/text 全空的节点，只有 bounds 不同。
st = make_state(clickable=[{"i": 0, "r": "item", "c": [10, 10]},
                           {"i": 1, "r": "item", "c": [20, 20]}],
                target="rid=item")
out = nd.strong_target_node(st)
check("没有给出动作", not out.get("action"))
check("给出了原因", bool(out.get("fallback_reason")),
      str(out.get("fallback_reason"))[:60])
check("原因说清是'不唯一'", "不唯一" in str(out.get("fallback_reason")))
check("路由去 decide", nd.route_after_strong({**st, **out}) == "decide")

print()
print("=" * 66)
print("  3) rid 找不到 → 交给模型")
print("=" * 66)
st = make_state(clickable=[{"i": 0, "r": "other", "c": [1, 1]}],
                target="rid=iv_more")
out = nd.strong_target_node(st)
check("没有给出动作", not out.get("action"))
check("原因说清是'没找到'", "没找到" in str(out.get("fallback_reason")),
      str(out.get("fallback_reason"))[:60])

print()
print("=" * 66)
print("  4) xpath 命中唯一 → 直接点（从落盘 XML 算）")
print("=" * 66)
tmpdir = Path(__file__).resolve().parents[1] / "tmp" / "_tmp_strong"
tmpdir.mkdir(parents=True, exist_ok=True)

xml_one = tmpdir / "one.xml"
xml_one.write_text(
    '<hierarchy rotation="0">'
    '<node resource-id="com.test:id/iv_more" clickable="true" bounds="[80,160][120,200]"/>'
    '<node resource-id="com.test:id/other" clickable="true" bounds="[200,300][240,340]"/>'
    '</hierarchy>', encoding="utf-8")

st = make_state(clickable=[], xml_path=str(xml_one),
                target='xpath=//*[@resource-id="com.test:id/iv_more"]')
out = nd.strong_target_node(st)
act = out.get("action") or {}
check("拿到动作", bool(act), str(act)[:70])
check("坐标是节点中心", (act.get("next_x"), act.get("next_y")) == (100, 180),
      f"{act.get('next_x')},{act.get('next_y')}")
check("记为 xpath 命中唯一",
      "xpath" in str([h.get("how") for h in (out.get("strong_hits") or [])]))

print()
print("=" * 66)
print("  5) ★ xpath 命中多个 → 交给模型")
print("=" * 66)
xml_many = tmpdir / "many.xml"
xml_many.write_text(
    '<hierarchy rotation="0">'
    '<node resource-id="com.test:id/item" clickable="true" bounds="[10,10][20,20]"/>'
    '<node resource-id="com.test:id/item" clickable="true" bounds="[30,30][40,40]"/>'
    '</hierarchy>', encoding="utf-8")

st = make_state(clickable=[], xml_path=str(xml_many),
                target='xpath=//*[@resource-id="com.test:id/item"]')
out = nd.strong_target_node(st)
check("没有给出动作", not out.get("action"))
check("原因说清'不唯一'", "不唯一" in str(out.get("fallback_reason")),
      str(out.get("fallback_reason"))[:60])

print()
print("=" * 66)
print("  6) ★ 落点预判只在本步没动过手时生效（防'起点即终点'）")
print("=" * 66)
# 起点就在 A，落点是 A —— 第 0 轮判"已满足"是对的。
st = make_state(clickable=[], landing="activity=A", round_in_step=0)
out = nd.strong_target_node(st)
check("第 0 轮：落点已满足 → 判完成",
      (out.get("action") or {}).get("step_done") is True)

# 但一旦动过手（round_in_step > 0），就不能再靠落点判完成 ——
# 否则"从 A 到 A"的用例会一步不走却报 PASS（loopblock 实测撞到过）。
st = make_state(clickable=[{"i": 0, "r": "x", "c": [1, 1]}],
                landing="activity=A", round_in_step=3)
out = nd.strong_target_node(st)
check("第 3 轮：不再用落点判完成", not (out.get("action") or {}).get("step_done"))
check("而是交给模型", bool(out.get("fallback_reason")) or
      (out.get("action") or {}).get("next_x") is not None)

print()
print("=" * 66)
print("  7) 没有 target → 交给模型（不是沉默跳过）")
print("=" * 66)
st = make_state(clickable=[{"i": 0, "r": "x", "c": [1, 1]}])
out = nd.strong_target_node(st)
check("没有给出动作", not out.get("action"))
check("原因说清'没有声明 target'",
      "target" in str(out.get("fallback_reason")).lower(),
      str(out.get("fallback_reason"))[:60])

print()
print("=" * 66)
print("  8) 处境说明里带上 desc / pitfalls（模型靠它理解意图）")
print("=" * 66)
st = make_state(clickable=[])
st["task"]["steps"][0]["desc"] = "点右上角「更多」按钮，菜单选「课程表」"
st["task"]["steps"][0]["pitfalls"] = ["iv_more 没有 text/desc"]
out = nd.strong_target_node(st)
guid = str(out.get("fallback_guidance") or "")
check("带上了具体 desc", "点右上角" in guid)
check("带上了坑", "iv_more" in guid, guid[:80].replace("\n", " "))

print()
print("=" * 66)
print("  9) ★ YAML 里禁止坐标：扫描器必须抓到")
print("=" * 66)
from android_agent.cases.loader import find_stale_patterns

check("抓到坐标",
      bool(find_stale_patterns({"steps": [{"do": [{"tap": [100, 200]}]}]})))
check("抓到 locate_seq",
      bool(find_stale_patterns({"steps": [{"locate_seq": ["rid=a", "rid=b"]}]})))
check("干净的 YAML 不误报",
      not find_stale_patterns({"steps": [{"id": "s1", "target": "rid=x",
                                          "landing": "activity=A",
                                          "pitfalls": ["注意 iv_more"]}]}))

print()
print("=" * 66)
print("  10) ★★ 真机 168 暴露的 bug：once 落回模型就再也不回代码")
print("=" * 66)
# 现象：真机跑 168，80 轮**全是 decide**，代码直点 0 次。
# 根因：fallback_reason 一旦置上就被 plan 一直保留，
#       route_after_plan 于是永远去 decide，strong_target 再也不被调用。
_base = {
    "task": {"steps": [{"id": "s1", "desc": "点更多", "target": "rid=iv_more",
                        "landing": "activity=Timetable"}],
             "package": "com.test"},
    "step_index": 0,
    "last_step_id": "s1",
    "round_in_step": 2,
    "last_activity": "A",
    "last_package": "com.test",
    "obs_activity": "A",
    "obs_clickable": [{"r": "iv_more", "c": [10, 20]}],
    "obs_fresh": True,
    "findings": [], "issues": [], "progress": [], "visited": [],
}

# ① 同一屏上，已交给模型 -> 继续走模型（不跟模型抢，避免来回拉锯）
_st_same = {**_base, "fallback_reason": "代码认不出",
            "fallback_screen": nd._screen_fingerprint(_base)}
_out = nd.plan_node(_st_same)
check("同屏已交给模型 -> 保留 fallback",
      bool(_out.get("fallback_reason")), str(_out.get("fallback_reason")))
check("同屏 -> 路由去 decide", nd.route_after_plan({**_st_same, **_out}) == "decide")

# ② ★ 换屏了 -> 必须重新给代码一次机会（这是修的那个 bug）
_st_new = {**_st_same, "last_activity": "B", "obs_activity": "B"}
_out2 = nd.plan_node(_st_new)
check("★ 换屏 -> 清掉 fallback", not _out2.get("fallback_reason"),
      str(_out2.get("fallback_reason")))
check("★ 换屏 -> 路由回 strong_target",
      nd.route_after_plan({**_st_new, **_out2}) == "strong_target")

print()
print("=" * 66)
print("  11) ★ round_in_step 只在换步时归零（否则落点预判永远成立）")
print("=" * 66)
_out3 = nd.plan_node({**_base, "round_in_step": 5})
check("同一步内不被清零", _out3.get("round_in_step") == 5,
      str(_out3.get("round_in_step")))
_out4 = nd.plan_node({**_base, "last_step_id": "s0", "round_in_step": 5})
check("换步才归零", _out4.get("round_in_step") == 0,
      str(_out4.get("round_in_step")))

print()
print("=" * 66)
print("  全部通过" if ok_all else "  存在失败项")
print("=" * 66)
sys.exit(0 if ok_all else 1)
