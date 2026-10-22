"""开发期热调试工具 —— 不要再为了验证一小段逻辑重跑整条用例。

## 为什么要这个东西

调试 182 时的真实成本：
    改一行代码 -> 冷启动 App(5s) -> 过引导(3s) -> 权限框 -> 走到课程表(30s)
              -> 模型 80 轮(每轮 10~20s) -> 才发现改错了
一次 5~15 分钟，而且**大头的等待跟要验证的那行代码毫无关系**。

真正想验证的往往只是一件事：
    "这段解析对不对？" "这个控件找不找得到？" "这个点击能不能跳过去？"
这些都不需要跑全流程。

## 用法（三种粒度，由快到慢）

    # 1) 纯逻辑：不碰设备，毫秒级。改完立刻重跑。
    python dev_probe.py logic 182

    # 2) 单点试触：把设备摆到位，只做一次动作，看界面怎么变。
    #    设备已在目标页的话，几秒钟出结果。
    python dev_probe.py tap --rid btnCreateManually
    python dev_probe.py tap --rid btnCreateManually --repeat 2

    # 3) 走位到指定页面（慢，但只做一次；之后用 2) 反复试）
    python dev_probe.py goto timetable
    python dev_probe.py goto home

    # 4) 跑某条用例的 prepare —— 只验准备阶段，不跑断言
    python dev_probe.py prep 182

## 关键设计

* **复用真实工具层**（tools_bridge.RealDevice），不另写一套 adb 封装。
  否则"调试时能过、正式跑不过"—— 之前的坑有一半就是这么来的。
* **不写用例报告**，不污染 report_*.json / run_*.log。
* 每个子命令都打印**前后界面**，让"到底动没动"一眼可见。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..cases.paths import skill_root
from .bridge import RealDevice, ToolError

SKILL_ROOT = skill_root()


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------

def _open_device() -> RealDevice:
    """开会话并返回设备。

    ⚠️ 工具层要求**先开会话**才能 observe/act：
        "Audited device calls require a session; use session start first"
    方法名是 `start_session`（不是 session_start —— 记错过一次，
    结果所有 observe 都报上面那句，白排查半天）。
    """
    dev = RealDevice(skill_root=SKILL_ROOT)
    # title 会进工具层的会话记录，写清楚是调试用的，
    # 免得日后翻证据时把调试动作当成正式用例执行。
    dev.start_session("dev_probe 热调试")
    return dev


def _where(dev: RealDevice) -> tuple[str, list[dict]]:
    """当前 activity + 可点项。

    ⚠️ 会话可能在脚本跑到一半时失效（工具层会在检测到 App 崩溃/
    recording 不完整时 terminate_session）。这里遇到就**重开一次**会话再读 ——
    调试工具的目的是"把人送到能验证的状态"，不该因为一次会话失效就整个退出。
    重开只影响本调试会话，不改设备状态。
    """
    try:
        out = dev.observe_raw() or {}
    except ToolError as exc:
        msg = str(exc)
        if "session" not in msg.lower():
            raise SystemExit(f"读不到界面：{msg}")
        print(f"   （会话失效，重开：{msg[:60]}…）")
        try:
            dev.start_session("dev_probe 热调试（重开）")
        except Exception as exc2:  # noqa: BLE001
            raise SystemExit(
                f"重开会话也失败：{exc2}\n"
                f"多半是上一个会话有未闭合的调用，先用 tools/session.py finish 收尾。")
        out = dev.observe_raw() or {}
    return str(out.get("activity") or ""), out.get("next") or []


def _find(nexts: list[dict], rid: str) -> dict | None:
    rid = rid.split("/")[-1]
    for n in nexts:
        if str(n.get("rid") or "").split("/")[-1] == rid or str(n.get("r") or "") == rid:
            return n
    return None


def _center(n: dict) -> tuple[int, int]:
    c = n.get("center") or n.get("c") or []
    return int(c[0]), int(c[1])


# --------------------------------------------------------------------------
# 1) 纯逻辑：不碰设备
# --------------------------------------------------------------------------

def cmd_logic(case: str) -> int:
    """把"解析"这一层单独验一遍 —— 不连设备，毫秒级。"""
    from ..cases import loader as cases_loader
    from ..agent import nodes as ca

    got = cases_loader.load_case_by_name(case)
    spec = got[0] if isinstance(got, tuple) else got
    task = getattr(spec, "task", spec)
    precs = (task.get("preconditions") if isinstance(task, dict)
             else getattr(task, "preconditions", [])) or []

    print(f"=== 用例 {case}：{len(precs)} 条前提 ===\n")
    problems = 0
    for p in precs:
        g = (lambda k, d=None: p.get(k, d)) if isinstance(p, dict) \
            else (lambda k, d=None: getattr(p, k, d))
        kind, desc = g("kind"), g("desc") or ""
        print(f"[{kind}] {desc[:50]}")
        print(f"   skip_if = {g('skip_if')}")

        do = g("do") or []
        items = do if isinstance(do, list) else [do]
        for it in items:
            if not isinstance(it, str):
                continue
            prose = ca._looks_like_prose(it)
            acts = ca._parse_prepare_actions(it) if prose else []
            tag = "说明文字" if prose else "shell 命令"
            print(f"   do -> {tag}，解析出 {len(acts)} 个动作")
            for a in acts:
                print(f"        {a}")
            if prose and not acts and "点" in it:
                # 含"点"却解析不出动作 —— 多半是 rid 没写清楚
                print("        ⚠️ 这段提到了'点'但没解析出可执行动作，检查 rid 写法")
                problems += 1
        print()

    # 顺带把 skip_if 的判定能力也验一遍（离线，不依赖真实界面）
    print("=== skip_if 判定能力 ===")
    fake = {"next": [{"r": "cv_empty_content"}], "nodes_text": "课程表"}
    for key in ("has_node", "not_on_page", "all_of", "timetable_state"):
        r = ca._skip_if_met({key: "cv_empty_content"}, activity="X",
                            package="com.zui.calendar", obs=fake)
        print(f"   {key:16s} -> {r}   "
              f"{'（判不了，会保守照跑）' if r is None else ''}")

    print(f"\n{'✅ 没发现问题' if not problems else f'⚠️ {problems} 处需人工确认'}")
    return 0 if not problems else 1


# --------------------------------------------------------------------------
# 2) 单点试触
# --------------------------------------------------------------------------

def _tap_with_expect(dev: RealDevice, n: dict, label: str,
                     expect: str = "") -> bool:
    """点一下，并把**工具层的检查点结论**打出来。

    ★ 不用手写的"界面变了吗"来判断落点 —— 那正是 `child_agent.match_landing`
      以前犯过的错（手写版会让 `EditTimetableActivity` 误命中
      `TimetableActivity`）。这里一律走工具层 `--expect` 的 `checkpoint`。

    `expect` 空着也照点：第一轮探路本来就写不出落点，
    这时只报告 activity，由人决定要不要补进路径图。
    """
    x, y = _center(n)
    res = dev.act(x, y, why=f"dev_probe {label}", expect=expect or None)
    cp = getattr(res, "checkpoint", None) or {}
    line = f"   {label}: ({x},{y}) -> {getattr(res, 'activity', '?')}"
    if cp.get("checked"):
        line += "  ✅检查点命中" if cp.get("ok") else \
                "  ❌检查点未命中：" + "；".join(cp.get("missed") or [])
    elif expect:
        line += f"  ⚠️ 检查点没跑起来（expect={expect}）"
    print(line)
    return bool(res.ok) and (not cp.get("checked") or bool(cp.get("ok")))


def _path_expect(package: str, rid: str) -> str:
    """从路径图查这一步该落到哪（和主流程用的是同一个函数）。"""
    try:
        from ..agent import nodes as ca

        return ca._pathmap_expect(package, rid)
    except Exception:  # noqa: BLE001
        return ""


def cmd_tap(rid: str, repeat: int, gap_ms: int, package: str = "") -> int:
    dev = _open_device()
    act0, nexts = _where(dev)
    print(f"当前：{act0}")
    n = _find(nexts, rid)
    if not n:
        print(f"❌ 当前屏没有 {rid}。可点的有：")
        for x in nexts[:20]:
            print(f"     {x.get('rid') or x.get('r') or '(无 rid)':28s} "
                  f"t={str(x.get('t') or '')[:20]}")
        return 1

    print(f"命中 {rid} @ {_center(n)}  text={n.get('t') or ''!r}")
    exp = _path_expect(package, rid) if package else ""
    if exp:
        print(f"   路径图落点：{exp}")
    else:
        print("   路径图没有这条边的落点记录（第一轮探路，先看 activity）")

    ok = True
    for i in range(max(1, repeat)):
        if i:
            import time
            time.sleep(gap_ms / 1000.0)
        # 同一目标连点时，后续几次不带 expect：第一次过后界面应该已经变了，
        # 拿同一个落点反复校验只会误报（用户场景里这种情况很常见，
        # 比如"点两次才生效"的网格）。
        ok = _tap_with_expect(dev, n, f"第{i + 1}次", exp if i == 0 else "") and ok

    act1, _ = _where(dev)
    print(f"\n点击后：{act1}")
    print("✅ 界面变了" if act1 != act0 else "⚠️ 界面没变")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 3) 走位
# --------------------------------------------------------------------------

# `goto` 的目标就是路径图里的**节点名**（加一个 home 表示只回首页）。
# 不再在这里维护"怎么走"—— 怎么走是路径图的事。
_GOTO = {
    "home": [],
    "课程表页-空态": [],
    "课程表页-周视图": [],
    "日历首页": [],
}


_PKG = "com.zui.calendar"


def cmd_goto(target: str) -> int:
    """冷启动后按**路径图**走到目标节点。

    ★ 路线不写死在这里 —— 直接问工具层的 `pathmap.path_between`。
      路径图是唯一事实来源：它记着每个节点「认什么锚点 → 去哪个节点」，
      以及每个节点的识别键（用来生成 `--expect`）。
      以前这里手写了「iv_more → item_title」两步，那等于把路径图抄了一遍，
      抄错了没人知道。
    """
    import importlib
    import time

    from ..agent.nodes import _checkpoint      # 它负责把 tools 挂进 sys.path

    _checkpoint()
    pm = importlib.import_module("pathmap")

    nodes = pm.load(Path(SKILL_ROOT) / "knowledge" / "paths" / f"{_PKG}.md")
    if not nodes:
        print(f"❌ 没有 {_PKG} 的路径图")
        return 1
    print("路径图节点：" + "、".join(n["name"] for n in nodes))

    dev = _open_device()

    print("→ 冷启动（force-stop 后重新拉起）")
    dev.shell(f"am force-stop {_PKG}")
    time.sleep(1)
    dev.shell(f"monkey -p {_PKG} -c android.intent.category.LAUNCHER 1")
    time.sleep(5)

    # 冷启动的引导框：**能确定 rid 就点**（这和被测 App 自己的弹窗是一回事，
    # 用户原话："我们得弹窗都是我们自己包名。是流程得一部分…直接顺手点了就行"）
    for _ in range(4):
        act, nexts = _where(dev)
        if _find(nexts, "button1"):
            print(f"   过引导框「同意」  ({act})")
            _tap_with_expect(dev, _find(nexts, "button1"), "过引导框")
            time.sleep(3)
        elif _find(nexts, "timetable_guide_button"):
            print(f"   过引导页「我知道了」  ({act})")
            _tap_with_expect(dev, _find(nexts, "timetable_guide_button"), "过引导页")
            time.sleep(3)
        else:
            break

    if target == "home":
        act, _ = _where(dev)
        print(f"✅ 停在：{act}")
        return 0

    route = pm.path_between(nodes, "日历首页", target)
    if not route.get("found"):
        print(f"❌ 路径图里没有这条路：{route.get('reason')}")
        return 1
    print(f"路线：{' → '.join(route['nodes'])}（{len(route['steps'])} 步）")

    for st in route["steps"]:
        exp = pm.finish_expect(pm.find_node(nodes, st["to"]))
        act, nexts = _where(dev)
        # ★ 用工具层的 `edge_selector` 决定"认什么锚点"：
        #   同 rid 多态时（菜单里四个 item_title）它会把 text 一起带上
        #   —— `rid=item_title,text=课程表`。自己按 rid 找会命中第一个
        #   （可能是「搜索」而不是「课程表」），这正是用户在页面里
        #   反复点错的那类毛病。
        sel = pm.edge_selector(st)
        n = None
        for part in [p for p in sel.split(",") if p.strip()]:
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            if k == "rid":
                cand = _find(nexts, v)
                if cand:
                    n = cand
            elif k == "text":
                cand = next((x for x in nexts
                             if v in str(x.get("t") or "")), None)
                if cand:
                    n = cand
            elif k == "desc":
                cand = next((x for x in nexts
                             if v in str(x.get("d") or "")), None)
                if cand:
                    n = cand
        if n is None:
            print(f"❌ {st['anchor']}：当前屏（{act}）找不到 {sel}")
            return 1
        print(f"   {st['anchor']} → {st['to']}   （认 {sel}）")
        if not _tap_with_expect(dev, n, st["anchor"], exp):
            print("❌ 检查点未命中，停下（和 act steps 的行为一致）")
            return 1
        time.sleep(2)

    act, _ = _where(dev)
    print(f"✅ 停在：{act}")
    return 0


# --------------------------------------------------------------------------
# 4) 只跑 prepare
# --------------------------------------------------------------------------

def cmd_prep(case: str) -> int:
    """只验"准备阶段"：解析 -> 找控件 -> 执行，不跑步骤和断言。

    设备没走到位时会明确告诉你"控件不在"，而不是让你等 80 轮才发现。
    """
    from ..cases import loader as cases_loader
    from ..agent import nodes as ca

    got = cases_loader.load_case_by_name(case)
    spec = got[0] if isinstance(got, tuple) else got
    task = getattr(spec, "task", spec)
    precs = (task.get("preconditions") if isinstance(task, dict)
             else getattr(task, "preconditions", [])) or []

    dev = _open_device()
    act, nexts = _where(dev)
    print(f"当前：{act}\n")

    for p in precs:
        g = (lambda k, d=None: p.get(k, d)) if isinstance(p, dict) \
            else (lambda k, d=None: getattr(p, k, d))
        if g("kind") != "data":
            continue
        print(f"[prepare] {(g('desc') or '')[:44]}")
        do = g("do") or []
        items = do if isinstance(do, list) else [do]
        pkg = (spec.get("package") if isinstance(spec, dict)
               else getattr(spec, "package", "")) or ""
        for it in items:
            if not isinstance(it, str):
                continue
            acts = ca._parse_prepare_actions(it, package=pkg)
            if not acts:
                print(f"   解析不出动作，跳过：{it[:50]}")
                continue
            print(f"   解析到 {len(acts)} 个动作：{[a['rid'] for a in acts]}")
            ok, fail, issues = ca._run_prepare_actions(dev, acts)
            print(f"   结果：成功 {ok}，失败 {fail}")
            for s in issues:
                print(f"     ! {s}")
        print()

    act2, _ = _where(dev)
    print(f"结束后：{act2}")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("logic", help="纯逻辑验证（不碰设备，毫秒级）")
    p.add_argument("case")

    p = sub.add_parser("tap", help="在当前屏试点一个控件")
    p.add_argument("--rid", required=True)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--gap-ms", type=int, default=400)
    p.add_argument("--package", default=_PKG,
                   help="用它去路径图查落点；传空串则不带 expect")

    p = sub.add_parser("goto", help="把设备走到指定页面")
    p.add_argument("target", choices=sorted(_GOTO))

    p = sub.add_parser("prep", help="只跑某用例的 prepare 前提")
    p.add_argument("case")

    a = ap.parse_args()
    if a.cmd == "logic":
        return cmd_logic(a.case)
    if a.cmd == "tap":
        return cmd_tap(a.rid, a.repeat, a.gap_ms, a.package)
    if a.cmd == "goto":
        return cmd_goto(a.target)
    return cmd_prep(a.case)


if __name__ == "__main__":
    raise SystemExit(main())
