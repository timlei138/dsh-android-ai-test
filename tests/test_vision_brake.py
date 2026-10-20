# -*- coding: utf-8 -*-
"""视觉通道失败必须刹车，不能无限重问。

## 167 真机事故（2026-10-20）

跑 167 时日志里连续 16 轮出现：

    -> 无动作（模型既没给坐标、也没给输入/按键/完成）：
       {'step_done': False, 'vision_question': '菜单里四项分别是什么？'}

模型每次都在请求看图，视觉节点每次都没答上来，但**没有任何东西
阻止它再问一次**。最终白烧 100 多秒，靠模型瞎猜对一个坐标才走出来。

## 两层病根

① 额度：assets/tools/vision.py 的 --max-tokens 默认 500，
   推理模型把额度全花在 reasoning 上，返回空文本（ok 仍为 true）
② 没刹车：vision_node 失败后只写 issues，**action 里的
   vision_question 还在**，路由于是又送回 vision —— 死循环

只修 ① 不够：换个大图/换个模型还会再犯。
必须让"失败"变成一个能让流程转弯的事实。这条测试钉的就是 ②。
"""

import sys
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = FAIL = 0


def ck(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")


import os  # noqa: E402

os.environ.setdefault("ANDROID_SKILL_ROOT", str(ROOT / "assets"))

import android_agent.agent.nodes as nd  # noqa: E402
import android_agent.agent.vision as vmod  # noqa: E402
import android_agent.agent.decide as dc  # noqa: E402

print("=" * 70)
print("1) 额度：默认值够不够")
print("=" * 70)
ck("★ 默认 max_tokens >= 6000（2000 会被 reasoning 吃光）",
   vmod.DEFAULT_MAX_TOKENS >= 6000, str(vmod.DEFAULT_MAX_TOKENS))

tools_vision = (ROOT / "assets" / "tools" / "vision.py").read_text(encoding="utf-8")
import re  # noqa: E402

m = re.search(r'"--max-tokens",\s*type=int,\s*default=(\d+)', tools_vision)
ck("★ 命令行默认值也改了（原来 500）", m and int(m.group(1)) >= 6000,
   f"找到的是 {m.group(1) if m else '没有'}")

print()
print("=" * 70)
print("2) 刹车：失败到上限后必须摘掉 vision_question")
print("=" * 70)
ck("上限很小（不能跑到十几次才停）", 1 <= nd.VISION_MAX_FAILURES <= 3,
   str(nd.VISION_MAX_FAILURES))

base_action = {"step_done": False,
               "vision_question": "菜单里四项分别是什么？",
               "why": "需要看图"}

# 达到上限
st = {"action": dict(base_action), "last_shot": "/x/shot.png",
      "vision_failures": nd.VISION_MAX_FAILURES,
      "vision_error": "返回空文本（reasoning 吃光额度）"}
out = nd.vision_node(st)
act = out.get("action") or {}
ck("★ 到上限后 action 里没有 vision_question",
   "vision_question" not in act, str(act))
ck("★ 所以路由不会再回 vision",
   nd.route_after_decide({"action": act}) != "vision",
   nd.route_after_decide({"action": act}))
ck("★ 明确告知模型为什么停",
   any("停止看图" in str(i) for i in (out.get("issues") or [])),
   str(out.get("issues")))
ck("★ 保留了 why（模型还知道自己在干嘛）", act.get("why") == "需要看图")
ck("保留 step_done 字段", "step_done" in act)

# 没到上限：给一次重试机会
st2 = {"action": dict(base_action), "last_shot": "",
       "vision_failures": 0}
out2 = nd.vision_node(st2)
ck("第一次失败时计数变 1", out2.get("vision_failures") == 1,
   str(out2.get("vision_failures")))
ck("第一次失败仍保留 vision_question（允许重试）",
   "vision_question" in (out2.get("action") or {}))
ck("第一次失败也记 issue（不再静默）",
   bool(out2.get("issues")), "167 的病根之一就是失败完全静默")

print()
print("=" * 70)
print("3) 没有截图 ≠ 没问题，必须计数")
print("=" * 70)
st3 = {"action": dict(base_action), "last_shot": ""}
o = nd.vision_node(st3)
ck("★ 无截图时不再返回空 dict（原来就是这里静默）",
   o != {}, "以前 return {} 让 22 次失败完全隐身")
ck("无截图也算一次失败", o.get("vision_failures") == 1)
ck("说明原因", any("截图" in str(i) for i in (o.get("issues") or [])))

print()
print("=" * 70)
print("4) 没问问题时不打扰")
print("=" * 70)
o = nd.vision_node({"action": {"step_done": True}, "last_shot": "/x.png"})
ck("没有 vision_question 就返回空（不新增失败计数）", o == {}, str(o))

print()
print("=" * 70)
print("5) 成功之后失败计数要清零")
print("=" * 70)
# 用一个假的 ask_image 模拟成功（不真调网络）
#
# ⚠️ 不能只替换 nd.ask_image —— vision_node 里还先调了 _ctx()，
#    而子 Agent 上下文没注入时它直接抛 RuntimeError（这一层在
#    ask_image 之前）。所以 _ctx 也要一起打桩，否则测的根本不是
#    成功路径，而是"忘了注入 context"。
_orig = nd.ask_image
_orig_ctx = nd._ctx
nd._ctx = lambda: type("C", (), {"python": sys.executable,
                                 "skill_root": str(ROOT / "assets")})()
nd.ask_image = lambda *a, **k: "菜单里有：课程表、纵览、年视图、设置"
try:
    st4 = {"action": dict(base_action), "last_shot": "/x/shot.png",
           "vision_failures": 1, "vision_error": "上次空返回"}
    o = nd.vision_node(st4)
    ck("★ 成功后 vision_failures 归零", o.get("vision_failures") == 0,
       str(o.get("vision_failures")))
    ck("答案挂到 action.vision 上",
       o.get("action", {}).get("vision", "").startswith("菜单里"),
       str(o.get("action", {}).get("vision"))[:40])
    ck("vision_error 清空", o.get("vision_error") == "")
finally:
    nd.ask_image = _orig
    nd._ctx = _orig_ctx

print()
print("=" * 70)
print("6) state 里真的有这两个字段")
print("=" * 70)
ann = nd.ChildState.__annotations__
ck("vision_failures 已声明", "vision_failures" in ann)
ck("vision_error 已声明", "vision_error" in ann)

print()
print("=" * 70)
print("7) 日志别再把'要知识/要看图'说成'无动作'")
print("=" * 70)
runner = (ROOT / "src" / "android_agent" / "agent" / "runner.py").read_text(
    encoding="utf-8")
i = runner.find("无动作（模型既没给坐标")
seg = runner[max(0, i - 900):i + 300]
ck("★ 日志分支里区分了 need_knowledge",
   "need_knowledge" in seg, "167 里 need_knowledge 被打成'无动作'，排查时被带偏")
ck("★ 日志分支里区分了 vision_question", "vision_question" in seg)

print()
print("=" * 70)
print("8) ★★★ 看图的结果必须真的到达模型 ★★★")
print("=" * 70)
# 这是 167 空转的真正病根，比 max_tokens 更根本：
#   vision 算出答案 -> 写进 action -> 路由去 observe -> 重新 decide，
#   而 decide 的提示词里**从来没有** vision 字段。
#   模型只看得到自己问过什么，看不到答案，于是以为没看到，再问一次。
#   实测连问 21 次、烧掉 95 秒，而且因为视觉是"成功"的，
#   上面那个失败闸门压根没触发 —— 修了失败路径，坏的是成功路径。

ck("vision_node 成功后写 last_vision（会活过下一轮 observe）",
   "last_vision" in inspect.getsource(nd.vision_node))
ck("state 里有 last_vision",
   "last_vision" in nd.ChildState.__annotations__)

dsrc = (ROOT / "src" / "android_agent" / "agent" / "decide.py").read_text(
    encoding="utf-8")
ck("★ decide 的 payload 带上了 vision",
   'payload.get("vision")' in dsrc,
   "不带的话模型永远看不到自己问的答案")
ck("★ 提示词里明确说这是答案、别再问",
   "不要再问同一件事" in dsrc)

# 真跑一遍 decide_step 的提示词构造，确认答案真的会出现在提示词里
_built = {}


def _fake_chat_msg(cfg, msgs, **kw):
    _built["text"] = msgs[-1]["content"]
    return {"tool_calls": [{"function": {"name": "done", "arguments": "{}"}}]}


_orig_chat = dc._chat_msg
dc._chat_msg = _fake_chat_msg
try:
    dc.decide_step(sys.executable, str(ROOT / "assets"), {
        "kind": "decide", "mode": "step", "step_id": "s1",
        "step_desc": "打开菜单", "activity": "X", "clickable": [],
        "vision": "四个菜单项是：课程表、纵览、年视图、设置",
    })
finally:
    dc._chat_msg = _orig_chat
_ptxt = _built.get("text") or ""
ck("★ 视觉答案真的出现在提示词里", "课程表、纵览、年视图、设置" in _ptxt,
   f"提示词长度 {len(_ptxt)}")
ck("★ 提示词里标明了这是答案", "视觉模型已经看过" in _ptxt)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)

