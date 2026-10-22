# -*- coding: utf-8 -*-
"""子 Agent 能用的**工具**（给模型的 function calling 定义）。

## 为什么从 "让模型吐 JSON" 改成 "让模型调工具"

这就是用户问的那件事：「我们不能像 LangChain 之类的写 Tools 的方式调用吗」

### 旧做法（response_format=json_object）

提示词里给一段 JSON 形状说明，让模型自己拼：

    {"step_done": false, "next_x": 1520, "next_y": 375, "why": "..."}

问题在于**模型只能看到字段名，看不到"这些字段是几种不同的动作"**。
176 实测三件事都发生了：

    模型想的：  「名称是必填项，先输入「测试课程表」再点完成」← 判断完全正确
    模型给的：  {"next_x": 1520, "next_y": 375, "why": "先点进输入框…"}
    实际效果：  点了一下输入框。界面没变化（输入框本来就在那儿）。
    然后：      连点 3 次被熔断 → BLOCKED

它在 `why` 里说了"要输入"，但动作字段里**只有坐标能填**。
我在提示词里补了"③ 需要填文字 -> 给 input_rid"，那是在**请求**模型换字段，
靠的是它读得够仔细 —— 正是用户反复强调不该依赖的东西。

### 新做法（tools / function calling）

把动作定义成**几个名字不同的工具**，模型从签名里选：

    tap(x, y, why)              点一个控件
    type_text(rid, text, why)   往输入框里写字
    press_key(key)              按返回/回桌面
    done(why)                   这一步做完了
    need_knowledge(query)       想查知识卡
    ask_vision(question)        想看图
    give_up(reason)             这一步走不通

实测效果（同一个问题，同一个模型）：

    旧: {"next_x": 1520, "next_y": 375, "why": "先点进输入框…"}
    新: type_text(rid="et_schedule_name", text="测试课程表")   ← 一次就对

差别不在提示词写得多好，而在**动作的形状本身让模型没法选错**：
`type_text` 这个工具名就在告诉它"你要的是这个"。这是结构约束，不是自觉。

### 保持了原有的纪律

工具不是随便给的：坐标仍然由 `_validate_action` 校验（必须来自当前可点项），
工具只是换了模型的表达方式，没有放松任何一条安全约束。

## 为什么不用 langchain 那个包

我们要的只是 `tools` 数组 + `tool_calls` 解析，就是 OpenAI 协议本身。
引 langchain 会带进几十个依赖和它自己的抽象层，出问题时要跨库排查。
这里手写 40 行协议代码，行为一眼看得全。
"""

from __future__ import annotations

from typing import Any

# ══════════════════════════════════════════════════════════════════
# 工具定义（OpenAI function calling 格式）
# ══════════════════════════════════════════════════════════════════

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": (
                "这一步的目标已经达成了。用之前先对照「本步期望落点」确认，"
                "别因为界面上出现了相关文字就急着报完成。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "why": {"type": "string", "description": "为什么判定已完成（一句话）"},
                },
                "required": ["why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tap",
            "description": (
                "点屏幕上**一个控件**。只要说清点哪个（名字/rid），"
                "不要给坐标 —— 元素在哪由执行器自己从观察结果解析。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "要点的控件名（rid 或文字，从「当前可点项」原样抄）。"
                            "同名多个时用 `名字#序号`，如 cv_empty_content#1"),
                    },
                    "why": {"type": "string", "description": "为什么点它"},
                    "is_dialog": {
                        "type": "boolean",
                        "description": (
                            "点的这个是不是挡路的弹窗（权限说明、更新提示、"
                            "「我知道了」这类）？是就填 true。"),
                    },
                    "dialog_text": {"type": "string", "description": "弹窗上的主要文字"},
                    "grants_permission": {
                        "type": "boolean",
                        "description": "点的是不是系统权限框的「允许/拒绝」按钮",
                    },
                    "permission_name": {"type": "string", "description": "如 存储/相机/位置"},
                    "permission_decision": {"type": "string", "description": "allow / deny"},
                },
                "required": ["target", "why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": (
                "往**输入框**里填文字。看到必填输入框（框里写着「必填」「请输入」"
                "这类占位符）就用它，**不要用 tap 点输入框** —— 点输入框界面不会变，"
                "也不会有任何效果，填完了校验永远过不去。"
                "填完之后由下一轮再决定要不要点保存。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "rid": {
                        "type": "string",
                        "description": (
                            "输入框的 rid（取自可点项列表的 r 字段，"
                            "如 et_schedule_name）"),
                    },
                    "text": {"type": "string", "description": "要填进去的内容"},
                    "why": {"type": "string", "description": "为什么填这个值"},
                },
                "required": ["rid", "text", "why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": (
                "按系统键，不点屏幕。屏幕上找不到合适的控件、或需要退出当前页面/"
                "App 时用它。**不要**去点语义不明的图标。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": ["back", "home"],
                        "description": "back=返回上一层；home=回桌面",
                    },
                    "why": {"type": "string"},
                },
                "required": ["key", "why"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "need_knowledge",
            "description": (
                "想查知识卡（这个 App 的界面结构、控件 rid、历史踩过的坑）就用它。"
                "查到的内容下一轮会给你。不确定路径怎么走时比瞎点划算。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "why": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_vision",
            "description": (
                "看不清界面（文字截断、图标语义不明、需要看颜色/位置关系）时，"
                "让它去看截图。问题问得越具体越有用。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要看什么"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_up",
            "description": (
                "这一步确实走不通了，转入下一步。**只有真的试过且无路可走才用** —— "
                "放弃会记进报告，影响最终判定。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "为什么走不通"},
                },
                "required": ["reason"],
            },
        },
    },
]

# 断言阶段用另一套：只判断，不动作。
_VERDICT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "report_verdict",
            "description": "报告这条断言的判定结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "passed": {
                        "type": "boolean",
                        "description": "断言是否成立。**看不到证据就别猜**，走 unknown",
                    },
                    "actual": {"type": "string", "description": "实际看到的（引用屏幕原文）"},
                    "evidence": {"type": "string", "description": "证据出处"},
                    "confidence": {
                        "type": "string", "enum": ["high", "medium", "low"]},
                    "unknown": {
                        "type": "boolean",
                        "description": "判不了就填 true（不等于不通过）",
                    },
                    "note": {"type": "string"},
                },
                "required": ["passed", "actual"],
            },
        },
    },
]


def decide_tools() -> list[dict[str, Any]]:
    """给 decide 阶段用的工具清单。"""
    return _TOOLS


def verdict_tools() -> list[dict[str, Any]]:
    """给 assert 阶段用的工具清单。"""
    return _VERDICT_TOOLS


# ══════════════════════════════════════════════════════════════════
# tool_calls -> 老的 action dict
# ══════════════════════════════════════════════════════════════════

def tool_call_to_action(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """把一次工具调用翻译成图里认识的 action dict。

    ★ 为什么要翻译而不是让图直接认工具

    图里的 act_node / 坐标校验 / 死循环检测 / 账本全是按 action dict 写的，
    而且都有测试钉着。换协议只需要在**边界**上翻译一次，
    内部一律不动 —— 这样改坏的风险最小，也不会让两套动作表示并存。

    未知工具名抛错（由调用方当格式错误处理），不猜。
    """
    a = dict(args or {})
    why = str(a.get("why") or "")

    if name == "done":
        return {"step_done": True, "why": why or "模型判定本步完成"}

    if name == "tap":
        # 模型给**语义目标**，不给坐标。
        # tap_target 由 act 节点用 _resolve_strong_target 解析成 (x,y)。
        # 没有 target 就是格式错误 —— 不猜坐标，也不接受旧的 x/y 写法。
        tgt = str(a.get("target") or "").strip()
        if not tgt:
            raise ValueError("tap 必须给 target（要点的控件名/rid）")
        out = {"step_done": False, "why": why, "tap_target": tgt}
        for k in ("is_dialog", "dialog_text", "grants_permission",
                  "permission_name", "permission_decision"):
            if k in a and a[k] not in (None, ""):
                out[k] = a[k]
        return out

    if name == "type_text":
        rid = str(a.get("rid") or "").strip()
        return {
            "step_done": False,
            "input_rid": rid,
            # ⚠️ 空串也是合法内容（清空输入框），所以只判 None
            "input_text": a.get("text") if a.get("text") is not None else "",
            "why": why or f"往 {rid} 填内容",
        }

    if name == "press_key":
        return {"step_done": False, "key": str(a.get("key") or "back"), "why": why}

    if name == "need_knowledge":
        return {"step_done": False, "need_knowledge": str(a.get("query") or ""),
                "why": why or "查知识卡"}

    if name == "ask_vision":
        return {"step_done": False, "vision_question": str(a.get("question") or ""),
                "why": why or "需要看图"}

    if name == "give_up":
        return {"step_done": False, "give_up": True,
                "give_up_reason": str(a.get("reason") or "走不通")}

    if name == "report_verdict":
        return dict(a)

    raise ValueError(f"模型调用了不存在的工具：{name}")


def first_tool_call(message: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """从模型返回的 message 里取出第一个工具调用 (name, args)。

    没有 tool_calls 返回 None（调用方据此决定是否退回 JSON 解析）。

    ⚠️ arguments 是**字符串**（JSON 文本），要自己 loads。
    有些模型/网关会在里面塞 ```json 包裹，这里一并剥掉。
    """
    calls = message.get("tool_calls") or []
    if not calls:
        return None
    fn = (calls[0] or {}).get("function") or {}
    name = str(fn.get("name") or "")
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return name, raw
    txt = str(raw or "").strip()
    # 剥掉可能的 ```json ... ``` 包裹
    if txt.startswith("```"):
        txt = txt.strip("`")
        if "\n" in txt:
            txt = txt.split("\n", 1)[1]
        txt = txt.rsplit("```", 1)[0]
    if not txt:
        return name, {}
    import json as _json

    try:
        val = _json.loads(txt)
    except ValueError as exc:
        raise ValueError(f"工具 {name} 的参数不是合法 JSON：{txt[:200]}") from exc
    if not isinstance(val, dict):
        raise ValueError(f"工具 {name} 的参数不是对象：{txt[:200]}")
    return name, val
