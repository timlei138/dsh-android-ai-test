# -*- coding: utf-8 -*-
"""tools.py 的契约测试 —— 工具定义 + tool_calls 翻译。

## 这个测试守的是什么

176 卡住的根因不是"模型不聪明"，而是**动作的形状**：
模型只能给坐标，想输入却无处表达。改成 tools 之后模型一次就选对了。

所以要守住三件事：
  1. 工具清单里有 type_text，且它的说明明确劝退"用 tap 点输入框"
  2. 每次工具调用都能翻译成图认识的 action dict，**字段不丢**
  3. 未知工具名要报错，不许悄悄忽略（悄悄忽略 = 模型说的话没人听）

## 为什么值得单独一个测试文件

`decide.py` 管的是"怎么问模型"，`tools.py` 管的是"模型能用什么说话"。
后者一旦退化成少一个工具，表现是"模型又开始点输入框了"，
而这种退化在别处的测试里完全看不出来。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from android_agent.agent import tools as T  # noqa: E402

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


print("=" * 66)
print("工具清单")
print("=" * 66)
names = [t["function"]["name"] for t in T.decide_tools()]
print(f"  工具: {', '.join(names)}")
ck("有 type_text（填必填框的唯一正解）", "type_text" in names)
ck("有 tap", "tap" in names)
ck("有 done", "done" in names)
ck("有 give_up", "give_up" in names)
ck("有 press_key（退不出去时像人一样按返回）", "press_key" in names)
ck("有 need_knowledge", "need_knowledge" in names)

_tt = next(t for t in T.decide_tools() if t["function"]["name"] == "type_text")
ck("type_text 的说明里劝退用 tap 点输入框",
   "不要用 tap 点输入框" in _tt["function"]["description"],
   _tt["function"]["description"][:80])
ck("type_text 必填 rid/text/why",
   set(_tt["function"]["parameters"]["required"]) == {"rid", "text", "why"})

_tap = next(t for t in T.decide_tools() if t["function"]["name"] == "tap")
# ★ 契约变了：模型不再给坐标，只给语义目标 target。
#   「执行器只决定下一步做什么，元素在哪由观察层用代码解析」。
ck("tap 必填 target/why（不再要坐标）",
   set(_tap["function"]["parameters"]["required"]) == {"target", "why"})
ck("★ tap 的 schema 里没有 x/y（不给模型算坐标的机会）",
   "x" not in _tap["function"]["parameters"]["properties"]
   and "y" not in _tap["function"]["parameters"]["properties"])

print()
print("=" * 66)
print("tool_calls -> action   翻译")
print("=" * 66)
a = T.tool_call_to_action("type_text",
                          {"rid": "et_schedule_name", "text": "测试课程表",
                           "why": "必填"})
ck("type_text -> input_rid", a.get("input_rid") == "et_schedule_name")
ck("type_text -> input_text", a.get("input_text") == "测试课程表")
ck("type_text 不带坐标", a.get("next_x") is None and a.get("next_y") is None)
ck("type_text 不会被当成完成", a.get("step_done") is False)

# 空串是合法内容（清空输入框），不能被当成"没给"
a = T.tool_call_to_action("type_text", {"rid": "x", "text": ""})
ck("空串 input_text 仍然透传（清空输入框是有效操作）",
   a.get("input_text") == "")

a = T.tool_call_to_action("tap", {"target": "课程表", "why": "进课程表"})
ck("★ tap -> tap_target（语义目标，不是坐标）",
   a.get("tap_target") == "课程表" and "next_x" not in a, str(a))
a = T.tool_call_to_action("tap",
                          {"target": "允许", "is_dialog": True,
                           "grants_permission": True,
                           "permission_name": "存储"})
ck("tap 的弹窗/权限申报透传",
   a.get("is_dialog") is True and a.get("grants_permission") is True
   and a.get("permission_name") == "存储")

# ★ 没有 target 就是格式错误 —— 不退回坐标、不猜
try:
    T.tool_call_to_action("tap", {"x": 1, "y": 2, "why": "旧格式"})
    ck("★ tap 缺 target -> 报错（不接受旧坐标格式）", False, "没有抛异常")
except ValueError as exc:
    ck("★ tap 缺 target -> 报错（不接受旧坐标格式）", "target" in str(exc), str(exc))

a = T.tool_call_to_action("done", {"why": "到目标页了"})
ck("done -> step_done=True", a.get("step_done") is True)

a = T.tool_call_to_action("press_key", {"key": "back", "why": "退出"})
ck("press_key -> key=back", a.get("key") == "back")

a = T.tool_call_to_action("need_knowledge", {"query": "课程表"})
ck("need_knowledge -> need_knowledge",
   a.get("need_knowledge") == "课程表")

a = T.tool_call_to_action("give_up", {"reason": "找不到"})
ck("give_up -> give_up=True", a.get("give_up") is True)

try:
    T.tool_call_to_action("fly_to_moon", {})
    ck("未知工具名必须报错", False, "居然没抛异常")
except ValueError as exc:
    ck("未知工具名必须报错", "fly_to_moon" in str(exc))

print()
print("=" * 66)
print("first_tool_call   解析")
print("=" * 66)
msg = {"content": "",
       "tool_calls": [{"function": {"name": "type_text",
                                    "arguments": '{"rid":"a","text":"b"}'}}]}
got = T.first_tool_call(msg)
ck("能从 tool_calls 取到 (name, args)",
   got is not None and got[0] == "type_text" and got[1]["rid"] == "a")

ck("没有 tool_calls 返回 None（调用方据此退回 JSON 解析）",
   T.first_tool_call({"content": "..."}) is None)

# 有些网关会塞 ```json 围栏
msg = {"tool_calls": [{"function": {
    "name": "tap", "arguments": '```json\n{"target":"课程表"}\n```'}}]}
got = T.first_tool_call(msg)
ck("能剥掉 ```json 围栏",
   got is not None and got[1].get("target") == "课程表")

try:
    T.first_tool_call({"tool_calls": [{"function": {
        "name": "tap", "arguments": "{坏掉的"}}]})
    ck("参数不是合法 JSON 要报错", False, "居然没抛异常")
except ValueError as exc:
    ck("参数不是合法 JSON 要报错", "不是合法 JSON" in str(exc))

print()
print("=" * 66)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
