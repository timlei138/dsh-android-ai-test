# -*- coding: utf-8 -*-
"""死循环必须由**代码**拦住，不能靠模型自觉。

## 案例（2026-10-21，176）

跑 176 时，s3 空转了 **65 轮、11 分钟**，在
「填名称 -> 点完成 -> 回到列表页 -> 再进编辑页」里循环，
在真机上建出 **6 张同名课程表**。起因是 YAML 把落点写错了一屏
（点完成落在列表页，不是周视图），但**真正该拦住它的是代码**。

## 漏洞在哪

`check_node` 里有收口：

    rounds = state["round_in_step"]
    if rounds >= limit:  -> BLOCKED 停

而且 `round_in_step` 在 act 里每轮都正常 +1。

但 `check_node` **只有在模型主动说 step_done 时才会被路由到**
（route_after_decide 里 `if a.get("step_done"): return "check"`）。
模型不说 done、一直在"努力"试，就永远进不了 check ——
那道上限**一次都没机会执行**。

`route_after_act` 的返回集合里根本没有 `"check"`。

教训：**上限写在某个节点里，就必须保证那条路由真的会到达它。**
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from android_agent.agent import nodes as nd  # noqa: E402

FAIL = 0
PASS = 0


def ck(name, cond, extra=""):
    global FAIL, PASS
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def st(**kw):
    base = {"action": {}, "obs_fresh": True,
            "fallback_reason": "模型接管", "round_in_step": 0,
            "max_rounds_per_step": 8}
    base.update(kw)
    return base


print("\n=== 1) route_after_act 必须在轮次超限时送去 check ===")

ck("★★ 9 轮 / 限 8 -> 送 check（收口）",
   nd.route_after_act(st(round_in_step=9)) == "check",
   nd.route_after_act(st(round_in_step=9)))
ck("★★ 恰好 8 轮 / 限 8 -> 也送 check（>= 语义）",
   nd.route_after_act(st(round_in_step=8)) == "check")
ck("★ 3 轮 / 限 8 -> 正常回 decide（没到上限别乱停）",
   nd.route_after_act(st(round_in_step=3)) == "decide")

print("\n=== 2) 图形里 act -> check 的边必须真的存在 ===")
# 返回 "check" 但映射表里没有，会直接抛 "Invalid route to node"
src = Path(nd.__file__).read_text(encoding="utf-8")
_act_block = src.split('b.add_conditional_edges("act", route_after_act', 1)
ck("★ 找得到 act 的条件边注册", len(_act_block) == 2)
if len(_act_block) == 2:
    mapping = _act_block[1].split("})", 1)[0]
    ck("★★ act 的映射表里有 check", '"check": "check"' in mapping,
       mapping[:200])
    ck("★ check 是真节点（映射到自己的名字）", "check" in mapping)

print("\n=== 3) 不能误伤正常路径 ===")
ck("看图请求优先于轮次上限（先 vision）",
   nd.route_after_act(st(round_in_step=99,
                         action={"vision_question": "这是什么"})) == "vision")
ck("缺画面时仍先去 observe",
   nd.route_after_act(st(obs_fresh=False, round_in_step=99)) == "observe")
ck("起点纠正优先",
   nd.route_after_act(st(start_mismatch=True, round_in_step=99)) == "start_check")
# 代码自己直点（没交模型）且没超限时，仍走 strong_target
ck("★ 代码直点路径没被破坏",
   nd.route_after_act(st(fallback_reason="", round_in_step=1)) == "strong_target")

print("\n=== 4) limit 取不到时要有安全默认值 ===")
ck("max_rounds_per_step 缺失 -> 用默认 8（不崩）",
   nd.route_after_act({"action": {}, "obs_fresh": True,
                       "round_in_step": 20}) == "check")
ck("max_rounds_per_step 是脏字符 -> 不抛异常",
   nd.route_after_act({"action": {}, "obs_fresh": True,
                       "round_in_step": 20,
                       "max_rounds_per_step": "abc"}) == "check")

print("\n=== 5) check_node 收到超限要能 BLOCKED（收口真的有效）===")
import inspect  # noqa: E402
_body = inspect.getsource(nd.step_check_node)
ck("★ check_node 里仍有 rounds 上限判断",
   "rounds >= limit" in _body or "rounds>=limit" in _body)
ck("★ 超限时返回 BLOCKED",
   "BLOCKED" in _body)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
