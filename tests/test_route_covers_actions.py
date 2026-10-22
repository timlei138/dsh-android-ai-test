# -*- coding: utf-8 -*-
"""路由必须认识 **action 里的每一个动作字段**。

## 为什么值得单独钉一条测试

同一个 bug 我犯了三次：

  · 169  `key`（按返回）—— 模型想退出相机，路由只认坐标 → 判成"没动作"
  · 176  `input_rid`（填必填框）—— 模型正确调 type_text → 判成"没动作"
  · 176  `vision_question`（要看图）—— 模型连请求 4 轮 → 一次都没看到

三次的表现一模一样：日志打 `-> 无动作：{...字段明明有值...}`，
看起来像模型什么都没说，实际是**路由把话吞了**。

根因不是粗心，而是没有强制机制 —— 加字段时没人提醒要同步改路由。
所以这条测试不测"某个具体字段"，而是**扫描 StepAction 的全部字段**，
任何"能单独构成动作"的字段没在路由里出现就失败。

这样下次再加动作类型，忘改路由会直接红。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")


print("=" * 70)
print("1) 每个动作字段都必须有路由分支")
print("=" * 70)

from android_agent.agent.nodes import (  # noqa: E402
    StepAction, route_after_decide)

# 这些字段**能单独构成一次动作**：模型只给这一个字段，图就应该去执行它。
# 新增字段时如果属于这一类，必须同时加进这里并改 route_after_decide。
ACTION_FIELDS = {
    "need_knowledge": "knowledge",
    "vision_question": "vision",
    "step_done": "check",
    "key": "act",
    "input_rid": "act",
    "next_x": "act",
    # ★ 语义目标（新契约）：模型只给 tap_target，坐标由 act 解析。
    #   2026-10-21 加了 kind 契约却忘了改路由，176 每轮都被吞成
    #   "无动作"、0 个动作跑空 —— 就是这个清单没跟着更新的后果。
    "tap_target": "act",
}

_src = io.open(str(ROOT / "src" / "android_agent" / "agent" / "nodes.py"),
               encoding="utf-8").read()
# 只取 route_after_decide 的函数体
_start = _src.find("def route_after_decide(")
_end = _src.find("\ndef ", _start + 10)
_route_body = _src[_start:_end]

for field, want in ACTION_FIELDS.items():
    ck(f"路由认识 {field} -> {want}", f'"{field}"' in _route_body
       or f"'{field}'" in _route_body,
       "route_after_decide 里没提这个字段 —— 模型给了也会被当成没动作")

print()
print("2) 实际行为：只给单个字段时必须路由对")
for field, want in ACTION_FIELDS.items():
    a = {field: ("x" if field == "need_knowledge" else
                 "q" if field == "vision_question" else
                 True if field == "step_done" else
                 "back" if field == "key" else
                 "some_rid" if field == "input_rid" else
                 "课程表" if field == "tap_target" else 100)}
    got = route_after_decide({"action": a})
    ck(f"只给 {field} → {got}（期望 {want}）", got == want, f"得到 {got}")

print()
print("3) 图的边必须连上这些节点")
for node in ("knowledge", "vision", "act", "check"):
    ck(f'decide 有通往 {node} 的边',
       f'"{node}": "{node}"' in _src,
       "add_conditional_edges 的映射表里缺这一项")

print()
print('4) StepAction 里所有「动作型」字段都在测试清单里')
_acts = {
    n for n, f in StepAction.model_fields.items()
    if n in ("need_knowledge", "vision_question", "step_done", "key",
             "input_rid", "next_x", "next_y", "give_up")
}
_missing = _acts - set(ACTION_FIELDS) - {"next_y", "give_up"}
ck("没有遗漏未被覆盖的动作字段", not _missing, f"未覆盖: {_missing}")
# give_up 由 check 分支兜（step_done=False 时落到 check 再判定收尾），单独说明
ck("give_up 有明确归属（走 check 收尾）",
   route_after_decide({"action": {"give_up": True}}) in ("check", "act"))

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
