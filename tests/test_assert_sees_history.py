# -*- coding: utf-8 -*-
"""断言判据必须看**过程**，不能只看最后一屏。

## 这一类 bug 我犯了四次，形状完全一样

    判据想知道"这一路上有没有出现过 X"，
    而实现只查"最后一刻 X 还在不在"。

  · seen_rids   178/168  a2  node_present  -> 末尾翻页了，判"不在"
  · seen_texts  168  a1      node_text     -> 末尾翻页了，判"不对"
  · seen_screens 177 a4      text_present  -> 末尾翻页了，判"没有"
                                （这次连 rid 都没有，是纯文本标签，
                                  前两个机制都救不了它）

断言在流程**末尾**统一判，那时人早走开了。所以凡是
"过程中出现过"语义的判据，都必须把"过程中"存下来。

## text_present 的语义到底是什么

是「**这个过程里屏幕上出现过这段文案**」，
不是「现在这一刻还在不在」。
想看"现在还在不在"应该用 node_present —— 那是另一个判据。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from android_agent.agent.nodes import (  # noqa: E402
    ChildState, evaluate_check)

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


def _st(**kw):
    base = {"obs_nodes_text": "", "seen_screens": [], "seen_rids": [],
            "obs_clickable": []}
    base.update(kw)
    return base


print("=" * 70)
print("1) text_present 要看整条过程，不只是最后一屏")
print("=" * 70)

# 177 a4 的真实情形：中间某屏有「周末是否有课」，末尾那屏已经没有
st = _st(obs_nodes_text="当前这屏是别的页面，没有那个标题",
         seen_screens=["第一屏：新建课程表 / 当前周数 / 第8周",
                       "第二屏：周末是否有课 / 显示非本周课程"])
ok, why = evaluate_check({"text_present": "周末是否有课"}, state=st)
ck("中间屏出现过 -> 成立", ok is True, f"得到 {ok} / {why}")

ok, why = evaluate_check({"text_present": "这条从没出现过"}, state=st)
ck("从没出现过 -> 不成立", ok is False, f"得到 {ok} / {why}")

st2 = _st(obs_nodes_text="最后一屏写着：保存成功")
ok, why = evaluate_check({"text_present": "保存成功"}, state=st2)
ck("只在最后一屏 -> 也成立", ok is True, f"得到 {ok} / {why}")

ok, why = evaluate_check({"text_present": ""}, state=st)
ck("空文案 -> 不成立（不能因为空串是任何串的子串就 PASS）",
   ok is False, f"得到 {ok}")

print()
print("2) 三种写法都要走同一条路（str / dict 无 rid / dict 带 equals）")
st3 = _st(obs_nodes_text="", seen_screens=["中间屏有：周末是否有课"])
for label, chk in [
    ("字符串形式", {"text_present": "周末是否有课"}),
    ("dict+text", {"text_present": {"text": "周末是否有课"}}),
    ("dict+equals", {"text_present": {"equals": "周末是否有课"}}),
    ("node_text 同义", {"node_text": {"text": "周末是否有课"}}),
]:
    ok, why = evaluate_check(chk, state=st3)
    ck(f"{label} 走历史屏", ok is True, f"得到 {ok} / {why}")

print()
print("3) 带 rid 的 node_text 语义不能被改坏（仍按那个控件自己的文案判）")
st4 = _st(obs_nodes_text="", seen_screens=["屏幕上别处写着 课程表"],
          obs_clickable=[{"r": "toolbar_title", "t": "日程"}],
          seen_texts=[{"rid": "toolbar_title", "text": "日程"}])
ok, why = evaluate_check(
    {"node_text": {"rid": "toolbar_title", "equals": "课程表"}}, state=st4)
ck("控件文案不符 -> 不成立（不能被整屏文案蒙混过关）",
   ok is False, f"得到 {ok} / {why}")
ok, why = evaluate_check(
    {"node_text": {"rid": "toolbar_title", "equals": "日程"}}, state=st4)
ck("控件文案相符 -> 成立", ok is True, f"得到 {ok} / {why}")

print()
print("4) state 字段确实存在（TypedDict 缺字段会让 reducer 静默不累积）")
fields = set(ChildState.__annotations__.keys())
ck("ChildState 有 seen_screens", "seen_screens" in fields)
ck("ChildState 有 seen_texts", "seen_texts" in fields)
ck("ChildState 有 seen_rids", "seen_rids" in fields)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
