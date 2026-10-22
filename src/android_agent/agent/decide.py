"""图内决策通道：把 decide/assert 两个判断点接到真模型。

为什么需要它：
  PoC 原版的决策者是 child_runner.py:autopilot_resume()，一个纯 Python if/else 规则函数。
  它能跑通 168，但遇到规则没覆盖的情况（没见过的弹窗、路径不对）就废了 ——
  用户的诉求是"像人类测试员一样"，找不到路能自己查文档走回去。

  这个模块把那两个判断点换成真模型调用：
    - decide  这一步做完了吗？下一步点哪？（含起点纠正模式）
    - assert  断言满足了吗？

设计约束（沿用 SKILL 的纪律）：
  1. 模型只能从**当前响应给出的可点项**里选坐标 —— 绝不自己造坐标。
     坐标必须来自当前 dump，复用旧坐标是 SKILL 里明确的坑。
  2. 子 Agent 可以主动要知识卡（need_knowledge），自己去查文档走回正确的路。
  3. 结构化输出：直接产出 StepAction / AssertVerdict 的 JSON，不解析自由文本。
  4. 失败要响亮：拿不到合法 JSON 就抛 DecisionError，不静默降级成"随便点一个"。

⚠️ 实测坑（沿用 vision_channel 的教训）：
  推理模型在 max_tokens 小时会把额度全花在 reasoning 上，正文返回空字符串。
  ⇒ 默认给足 2000，且空正文明确报错。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# 决策默认额度：够推理 + 出一个 JSON 动作。
# ⚠️ 实测（2026-10-16 真机）：给 2000 时出现过 completion_tokens=2000、
# reasoning_tokens 也=2000，正文被挤成空字符串。复杂界面（十几个可点项 +
# 知识卡 + 历史）推理量大，2000 不够。给 4000 留出余量。
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TIMEOUT = 180

# ★★ 断言专用额度（2026-XX-XX，178 真机实测）。
#
# 为什么断言不能沿用 DEFAULT_MAX_TOKENS=4000：
#   178 那次 usage 报得很清楚 ——
#       completion_tokens=6000, reasoning_tokens=6000
#   一个 token 都没留给正文。注意它比 4000 还高：说明 `_chat` 内部
#   遇到空正文会翻倍重试（4000 -> 8000，卡在 6000 是另一条路径），
#   但**推理能把 6000 也吃满**。
#
#   这不是"偶发"，是任务性质决定的：
#     · 选下一个控件   = 看一眼可点列表，挑一个      -> 4000 够
#     · 判定一条断言   = 读值 + 对照预期 + 组织判词  -> 明显更长的推理链
#   178 的 a2 要判「每节课上课时长默认 50 分钟」：得先在这屏找到那个控件、
#   读出它的值、再和 "50分钟" 逐字比对 —— 推理量是"找控件"的两倍以上。
#
# 代价对比（这就是为什么值得给大）：
#   给 4000 省下的那点 token，换来的是断言变成"判定缺失"（BLOCKED），
#   而用户看到的是"这条没验" —— 一条用例白跑，重新跑一次的成本
#   远高于多给几千 token。
ASSERT_MAX_TOKENS = 12000


class DecisionError(RuntimeError):
    """模型没给出可用决策。绝不吞掉——吞掉会变成"随便点一个"。"""


def _load_config(skill_root: str) -> dict[str, str]:
    """读 LLM 配置。

    优先用环境变量（方便换模型/换 key 而不动 skill 的文件），
    否则回落到 skill 的 storage/vision.json（本机已配 DeepSeek）。
    """
    base = os.environ.get("ANDROID_TEST_LLM_BASE") or ""
    model = os.environ.get("ANDROID_TEST_LLM_MODEL") or ""
    key = os.environ.get("ANDROID_TEST_LLM_KEY") or ""

    if not (base and model and key):
        conf_fp = Path(skill_root) / "storage" / "vision.json"
        try:
            conf = json.loads(conf_fp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DecisionError(
                f"没有可用的 LLM 配置：{conf_fp} 读不到或不是合法 JSON，"
                "且未设置 ANDROID_TEST_LLM_* 环境变量") from exc
        base = base or (conf.get("base_url") or "")
        model = model or (conf.get("model") or "")
        key = key or (conf.get("api_key") or "")

    if not (base and model and key):
        raise DecisionError("LLM 配置不完整（base_url / model / api_key）")
    return {"base_url": base.rstrip("/"), "model": model, "api_key": key}


def _llm_trace(title: str, body: str) -> None:
    """把提示词/回复原文打到日志（同时可落文件）。

    开关：环境变量 LLM_TRACE=0 可关掉（默认开）—— 排查手册就是靠这个。
    落文件：环境变量 LLM_TRACE_FILE=路径，追加写 markdown。

    为什么要**默认开**：这次的教训是"日志里只有结论，看不到问和答"，
    于是我们花了很久猜模型为什么不给 input_rid。原文有用。
    体积不用担心：一轮决定约 3-6KB，二三十轮也就百来 KB。
    """
    import os

    if os.environ.get("LLM_TRACE", "1") in ("0", "false", "False"):
        return
    # flush=True 是必须的：输出重定向到文件/管道时 Python 会块缓冲，
    # 不刷的话一次运行要跑完才看得见提示词，排查时等于没有。
    print(f"╭─ {title} " + "─" * max(0, 66 - len(title)), flush=True)
    for ln in body.rstrip("\n").splitlines():
        print(f"│ {ln}", flush=True)
    print("╰" + "─" * 70, flush=True)

    path = os.environ.get("LLM_TRACE_FILE")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n## {title}\n\n```\n{body.rstrip()}\n```\n")
        except OSError:
            pass


def _chat_msg(cfg: dict[str, str], messages: list[dict[str, Any]],
              max_tokens: int, timeout: int,
              tools: list[dict[str, Any]] | None = None,
              image: str | None = None, _retry: int = 2,
              trace: str = "") -> dict[str, Any]:
    """发一次 chat/completions，返回**完整的 message 对象**（含 tool_calls）。

    这是改造后的主入口：走 function calling，模型直接"调工具"而不是
    自己拼 JSON 字符串（见 tools.py 的说明）。

    返回 message dict，调用方用 tools.first_tool_call() 取 (name, args)。
    没有 tool_calls 时（某些模型/网关不保证），退回读 content 里的 JSON ——
    **两条路都留着**，不能因为服务端行为变化就让整条用例废掉。
    """
    return _chat_req(cfg, messages, max_tokens, timeout,
                     tools=tools, image=image, _retry=_retry, trace=trace)


def _chat(cfg: dict[str, str], messages: list[dict[str, Any]],
          max_tokens: int, timeout: int,
          image: str | None = None, _retry: int = 2,
          trace: str = "") -> str:
    """旧的"只要正文"入口。保留给还没改成 tools 的调用方。"""
    msg = _chat_req(cfg, messages, max_tokens, timeout, tools=None,
                    image=image, _retry=_retry, trace=trace)
    return str(msg.get("content") or "")


def _chat_req(cfg: dict[str, str], messages: list[dict[str, Any]],
              max_tokens: int, timeout: int,
              tools: list[dict[str, Any]] | None = None,
              image: str | None = None, _retry: int = 2,
              trace: str = "") -> dict[str, Any]:
    """发一次 chat/completions，返回**完整的 message 对象**。

    传了 tools 就走 function calling（模型回 tool_calls）；
    没传就退回 json_object 正文模式。

    _retry 是**剩余重试次数**（不是 bool）：空回复时逐级退避。
    0 表示不再重试，直接抛错。

    ★ 这里打印**完整提示词和完整回复**（用户要求）。
      在此之前日志里只有"模型决定点 (1520,375)：xxx"这一句结论，
      看不到我们到底问了什么、它到底答了什么 —— 排查"为什么不用 type_text"
      这种问题只能靠猜。现在两边原文都留。

      写去哪：默认打到 stdout（和进度日志一起），设了 LLM_TRACE_FILE
      就**追加写到那个文件**（一次运行一个 .md，排查时直接读）。
    """
    if image:
        # 把图塞进最后一条 user 消息
        try:
            b64 = __import__("base64").b64encode(
                Path(image).read_bytes()).decode("ascii")
        except OSError as exc:
            raise DecisionError(f"读图失败：{image}") from exc
        suffix = Path(image).suffix.lstrip(".").lower() or "png"
        if suffix == "jpg":
            suffix = "jpeg"
        last = dict(messages[-1])
        content = last.get("content")
        if isinstance(content, str):
            last["content"] = [
                {"type": "text", "text": content},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{suffix};base64,{b64}"}},
            ]
        messages = messages[:-1] + [last]

    _payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }

    # ★ 走 tools（function calling）时**不要**开 json_object。
    #
    # 两者是互斥的两条结构化输出路线：
    #   tools        -> 模型输出 tool_calls，参数由 schema 约束
    #   json_object  -> 模型输出纯 JSON 正文，形状靠提示词描述
    # 同时开会让模型在"该写工具调用"和"该写 JSON 正文"之间摇摆，
    # 实测出过 content 空 + tool_calls 也空的诡异回复。
    if tools:
        _payload["tools"] = tools
        _payload["tool_choice"] = "auto"
    else:
        # ★ 服务端会**硬性校验**：prompt 里不含 "json" 就直接 HTTP 400：
        #   "Prompt must contain the word 'json' in some form to use
        #    'response_format' of type 'json_object'."
        #   —— 这不是文档里的软建议，是实测撞出来的 400。
        #
        # 所以不能无条件开这个开关：哪天有人改了提示词、把 "json" 字样删了，
        # 整条用例会因为 400 挂掉，而错误信息在模型服务端，排查起来很绕。
        # 这里发送前自己先判一次：不满足就**不开** json_object（退回旧行为，
        # 仍能跑，只是少了服务端保证），而不是发出去撞 400。
        _all_text = "\n".join(
            str(m.get("content") or "") for m in messages).lower()
        # ⚠️ 带图时不开 json_object（178 a4 实测踩到）。
        #
        #   多模态 + response_format=json_object 在部分 OpenAI 兼容网关上
        #   是**不受支持**的组合：要么直接 400，要么返回空正文
        #   （reasoning 吃满额度、正文一个字都没写）。表现就是
        #   "模型回复无法解析（）"—— 看不出是格式问题还是模型没答。
        #
        #   断言本来就只输出一个简单 JSON，提示词里已经说清了形状，
        #   靠服务端强约束并非必需。带图时让正文自由输出，
        #   由 _extract_json 自己抠 —— 它对围栏/废话/裸引号都容忍。
        if "json" in _all_text and not image:
            _payload["response_format"] = {"type": "json_object"}

    body = json.dumps(_payload).encode("utf-8")

    # ★ 打印完整提示词（提问前）
    _msg_text = "\n\n".join(
        f"[{m.get('role')}]\n"
        + (m.get("content") if isinstance(m.get("content"), str)
           else json.dumps(m.get("content"), ensure_ascii=False))
        for m in messages)
    if tools:
        _msg_text += ("\n\n[可用的工具]\n"
                      + ", ".join(t["function"]["name"] for t in tools))
    _llm_trace(
        f"提示词 · {trace or 'decide'} · max_tokens={max_tokens}"
        + (f" · 重试剩余{_retry}" if _retry < 2 else ""),
        _msg_text)

    req = urllib.request.Request(
        f"{cfg['base_url']}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise DecisionError(f"LLM HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DecisionError(f"LLM 请求失败：{exc}") from exc

    try:
        msg = data["choices"][0]["message"] or {}
    except (KeyError, IndexError, TypeError) as exc:
        raise DecisionError(f"LLM 返回结构异常：{str(data)[:300]}") from exc

    text = str(msg.get("content") or "").strip()
    _calls = msg.get("tool_calls") or []

    # ★ 打印模型回复原文（拿到后）。
    #   有 tool_calls 就把调用**展开成人话**一起打 —— 排查时最想看的是
    #   "它这次选了哪个工具、参数是什么"，而不是一坨 JSON 转义。
    _usage = data.get("usage") or {}
    _shown = text
    if _calls:
        _lines = []
        for c in _calls:
            fn = (c or {}).get("function") or {}
            _lines.append(f"→ 调用工具 {fn.get('name')}"
                          f"({fn.get('arguments')})")
        _shown = (_shown + "\n" if _shown else "") + "\n".join(_lines)
    _llm_trace(
        f"模型回复 · {trace or 'decide'}"
        f" · tokens={_usage.get('total_tokens', '?')}"
        f"（推理 {_usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)}）",
        _shown or "(空回复)")

    # tool_calls 也是有效回复：正文为空不代表失败。
    if text or _calls:
        return msg

    usage = data.get("usage") or {}
    # 空回复 = reasoning 把额度吃光了。
    #
    # 实测：真机跑到第 7 轮偶发一次（completion=8000 全是 reasoning），
    # 单独复现 3 次却是 0/3 —— 是模型侧的**瞬时抖动**，不是 prompt 缺陷。
    #
    # 关键在于：这种抖动**不该让整条用例挂掉**。一次 OOM 式的空回复
    # 就把已经走了 7 轮的用例判死，代价完全不成比例。
    #
    # 所以退避策略要有**变化**，不能只是加倍：
    # temperature=0 下同 prompt 同额度，重试大概率还是空。
    #   第 1 次重试：加倍额度 + 明确催它直接给结论（压 reasoning）
    #   第 2 次重试：换成极简 prompt（去掉长上下文）
    if _retry >= 1:
        hard = messages + [{
            "role": "user",
            "content": ("直接调用工具回答，不要展开分析。"
                        "立即给出工具调用，不要任何解释。")}]
        # ⚠️ 额度必须**至少**翻倍且不低于 8000 —— 压 reasoning 的同时
        # 还得留够写完整参数的空间。实测踩到：额度被 reasoning 吃光后
        # JSON 只写了一半（"next_y": 就断了），整条用例因此挂掉。
        return _chat_req(cfg, hard, max(max_tokens * 2, 8000), timeout,
                         tools=tools, image=image, _retry=_retry - 1,
                         trace=trace)
    raise DecisionError(
        "LLM 返回空回复（通常是 max_tokens 被 reasoning 吃光）。"
        f"usage={usage}，当前 max_tokens={max_tokens}，建议调大。")


def _escape_inner_quotes(s: str) -> str:
    """把 JSON 字符串值里**没转义的裸双引号**转成 \\"。

    为什么需要（实测踩到）：
      模型判定「图库导入入口可点」时回了
        {"passed": true, "reason": "看到 "拍摄于 2026年8月7日 ...的照片""}
      内层引号是裸的 —— 严格来说这句 JSON 就是坏的，但**模型的意图完全清楚**。
      旧实现两层 json.loads 都失败后，落到正则抢救，而抢救只认标量字段，
      `"reason"` 被截成 `"看到 "` 还**静默当成成功**返回了。
      结果是：一条本来 PASS 的断言，要么让整个用例 exit=1，要么给出残缺值。

    做法：扫一遍字符，只在「字符串值内部」把未转义的 " 变成 \"。
    已经转义过的（\\" / \\\\）原样保留，不会二次转义。
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        # 在字符串内部
        if ch == "\\":
            # 转义序列整体保留（含 \\" 和 \\\\）
            out.append(ch)
            if i + 1 < n:
                out.append(s[i + 1])
                i += 2
            else:
                i += 1
            continue

        if ch == '"':
            # 这是"字符串结束"还是"内部裸引号"？
            # 看后面第一个非空白字符：是 , } ] : 就当作结束，否则是裸引号。
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j >= n or s[j] in ",}]:":
                out.append('"')
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def _extract_json(text: str) -> dict[str, Any]:
    """从回复里抠出 JSON 对象。容忍 ```json 围栏、前后废话、字符串内的裸引号。"""
    s = text.strip()
    # ★ 空正文单独报，别和"格式不对"混成一句。
    #   实测踩到（178 a4）：报告里只写"模型回复无法解析（）"，
    #   看不出到底是模型没答、还是答了但格式坏 —— 这两种修法完全不同。
    if not s:
        raise DecisionError(
            "模型返回了空正文（不是格式问题）—— 通常是 reasoning 把 "
            "max_tokens 吃光了，或者返回的是 tool_calls 而正文为空。")
    if s.startswith("```"):
        # 去掉围栏
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()

    candidates = [s]
    # 退一步：找第一个 { 到最后一个 }
    lo, hi = s.find("{"), s.rfind("}")
    if lo != -1 and hi > lo:
        candidates.append(s[lo:hi + 1])

    for cand in candidates:
        # 第 1 轮：原样解析
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        # 第 2 轮：修掉字符串内的裸引号再解析
        try:
            obj = json.loads(_escape_inner_quotes(cand))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 再退一步：**正文被 max_tokens 截断**时抢救已知字段。
    # 实测踩到：退避重试把额度用在了别处，JSON 写一半就断了
    # （`"next_y":` 后面没了），整个用例因此挂掉 —— 明明点击意图已经清楚。
    # 这里只捞**能安全确定**的标量字段，捞不到就让上层报错，不瞎猜。
    #
    # ⚠️ 正则里必须用 (?:[^"\\]|\\.)* 而不是 [^"]* —— 后者遇到裸引号会在
    #    "看到 " 就断掉，把残缺值当成功返回（就是上面那个 bug 的一半）。
    salvaged: dict[str, Any] = {}
    body = s[lo:] if lo != -1 else s
    for m in re.finditer(
            r'"([a-z_]+)"\s*:\s*(true|false|-?\d+|"(?:[^"\\]|\\.)*")', body):
        k, v = m.group(1), m.group(2)
        if v in ("true", "false"):
            salvaged[k] = (v == "true")
        elif v.startswith('"'):
            salvaged[k] = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            salvaged[k] = int(v)
    # 只有**坐标和判定**都齐了才算抢救成功（缺坐标等于没决定）
    if "step_done" in salvaged and (
            salvaged["step_done"] or
            ("next_x" in salvaged and "next_y" in salvaged)):
        salvaged.setdefault("_salvaged_from_truncated_json", True)
        return salvaged

    raise DecisionError(f"模型没给出合法 JSON：{text[:300]}")


# 7 类输出的名字。写死在这里，和 _SYSTEM 里的说明、tools 定义必须一致。
_KINDS = ("tap", "input", "key", "done", "knowledge", "vision", "give_up")


def action_from_kind(obj: dict[str, Any]) -> dict[str, Any]:
    """把「带 kind 的分类 JSON」翻译成图里认识的 action dict。

    这是模型**没走 function calling、改用 JSON 正文**时的那条路。
    要求模型吐 `kind` + 语义目标，和 tools 那条路**落到同一个 action dict**，
    下游完全不用改。

    没有 kind = 格式错误，**不猜**。以前允许它直接吐 next_x/next_y，
    等于让模型自己算坐标 —— 格式说不清、错了也不知道错在哪，现在不要了。
    """
    kind = str(obj.get("kind") or "").strip()
    if not kind:
        raise DecisionError(
            f"回复里没有 kind 字段。必须输出 7 类之一"
            f"（{'/'.join(_KINDS)}），原回复：{str(obj)[:200]}")
    if kind not in _KINDS:
        raise DecisionError(
            f"不认识的 kind={kind!r}。合法的只有：{'/'.join(_KINDS)}")
    why = str(obj.get("why") or "")
    tgt = str(obj.get("target") or "").strip()

    if kind == "done":
        return {"step_done": True, "why": why or "模型判定本步完成",
                "_by_kind": True}

    if kind == "give_up":
        return {"step_done": False, "give_up": True,
                "give_up_reason": str(obj.get("reason") or why or "走不通"),
                "_by_kind": True}

    if kind == "key":
        return {"step_done": False, "key": str(obj.get("key") or "back"),
                "why": why, "_by_kind": True}

    if kind == "knowledge":
        return {"step_done": False,
                "need_knowledge": str(obj.get("query") or why),
                "why": why or "查知识卡", "_by_kind": True}

    if kind == "vision":
        return {"step_done": False,
                "vision_question": str(obj.get("question") or why),
                "why": why or "需要看图", "_by_kind": True}

    if kind == "input":
        if not tgt:
            raise DecisionError("kind=input 必须给 target（输入框的 rid）")
        return {"step_done": False, "input_rid": tgt,
                "input_text": obj.get("text") if obj.get("text") is not None else "",
                "why": why or f"往 {tgt} 填内容", "_by_kind": True}

    # kind == "tap"：★ 只带语义目标，不带坐标。
    # 坐标由 strong_target 那套（_resolve_strong_target）解析，
    # 解析不到就是不唯一/不存在 —— 报错让模型换目标，而不是猜一个位置。
    if not tgt:
        raise DecisionError("kind=tap 必须给 target（可点项的名字或 rid）")
    out = {"step_done": False, "why": why, "_by_kind": True,
           # tap_target 是"要点的语义目标"，act 节点据此解析坐标
           "tap_target": tgt}
    for k in ("is_dialog", "dialog_text", "grants_permission",
              "permission_name", "permission_decision"):
        if k in obj and obj[k] not in (None, ""):
            out[k] = obj[k]
    return out


# ── 提示词 ──────────────────────────────────────────────────────────────────

_SYSTEM = """你是一个 Android 真机测试执行者，像人类测试员一样操作手机。

## 你的职责边界（先看这条）

**你只决定"下一步做什么"，不负责"元素在哪"。**
当前屏幕上有什么、每个可操作项叫什么、在哪个坐标 —— 观察层已经用代码
解析好了，放在「当前可点项」里。你从里面**挑出要操作的那一个**，用它的名字，
坐标由执行器自己解析。

所以你**永远不要输出坐标**，也不要自己算位置。你只需要说清楚：
对**哪一个**（名字/rid）做什么动作。

## 全部合法输出（7 类，每类一个固定 JSON）

每次回复**只能是下面 7 种之一**，必须是**单个 JSON 对象**，顶层必须有 `"kind"`。
`"kind"` 之外**不要加任何别的键** —— 多出来的键会被丢弃，不会报错，但也没用。

### 1. tap —— 点一个控件
```json
{"kind": "tap", "target": "完成的控件名或 rid", "why": "为什么点它"}
```
- `target` **必须**是「当前可点项」里某一项的名字（rid 或 text），原样抄。
- 如果同一个名字出现多次，用 `名字#序号` 指明第几个（序号从 1 开始）。
- 点弹窗/权限框时，额外交代这三项（会被记进报告供审计）：
  ```json
  {"kind": "tap", "target": "允许", "why": "放行通知权限",
   "is_dialog": true, "grants_permission": true,
   "permission_name": "通知", "permission_decision": "allow"}
  ```
  点的是 App 自绘的「权限说明页」但按钮写「同意」，同样算 `grants_permission=true`。

### 2. input —— 往输入框填字
```json
{"kind": "input", "target": "输入框的 rid", "text": "要填的内容", "why": "为什么填"}
```
- 填**空字符串**是合法的（表示清空）。
- 填完不要再重复填：下一轮会告诉你框里现在是什么。**只有值不对才重填。**

### 3. key —— 按系统键
```json
{"kind": "key", "key": "back", "why": "退回上一页"}
```
`key` 只能是 `back` 或 `home`。

### 4. done —— 本步做完了
```json
{"kind": "done", "why": "现在看到 xxx，符合本步目标"}
```
判据是**本步的落点/目标**。到了就立刻 done，**不要在目标页上继续点**。
如果本步坑位写了"到此为止"，更是硬边界。

### 5. knowledge —— 想查知识卡
```json
{"kind": "knowledge", "query": "课程表 入口", "why": "不确定路径"}
```
查到的内容下一轮给你。

### 6. vision —— 想让视觉模型看图
```json
{"kind": "vision", "question": "屏幕顶部标题区写的是什么？有没有弹窗？"}
```
**只在 XML 真的看不出来时用**（图标含义不明、文字被截断、需要看颜色/位置）。
问题要问得**具体到能用一个事实回答**，不要问"现在是什么界面"这种开放问题。

### 7. give_up —— 真的走不通了
```json
{"kind": "give_up", "reason": "为什么走不通"}
```
**只有确实试过且无路可走才用**。放弃会进报告，影响最终判定。

## 行为铁律

1. 只从「当前可点项」里选目标，**不编造**名字。
2. 目标不在列表里 → 先滚动、先返回、先关掉挡路的弹窗，别硬点。
3. 遇到规则外的弹窗（权限说明、引导页、更新提示等）→ **直接点掉再继续**，
   通常在「同意/确定/允许/继续」里选。同时按上面第 1 类如实申报。
4. **不要重复做同一个无效动作**。如果提示某个动作做了 N 次界面毫无变化，
   说明缺前置条件（必填没填、开关没开），换思路；再重复就是拿设备陪跑。
5. 不确定时优先 `knowledge` 查知识卡，比瞎点划算。

## 输出格式硬要求

- 以 `{` 开头、`}` 结尾；**不要** ```json 围栏，不要任何前言、解释、后缀。
- 键和字符串值都用双引号。
- 字符串里要出现双引号必须转义成 \\"；更安全的做法是用「」代替。
- 只输出一个 JSON 对象，**不要**输出数组，不要输出多行自然语言。
"""


# 断言阶段是**另一套契约**：只判断，不动作，所以不认上面那 7 类。
_SYSTEM_VERDICT = """你是一个 Android 真机测试的断言判定者。

**你只判断事实，不操作手机。** 屏幕上有什么，观察层已经给你了；
你要回答的是「这条预期成立不成立」，并说明依据。

## 输出（只有一个 JSON 对象）

```json
{
  "kind": "verdict",
  "passed": true,
  "actual": "实际看到了什么（必须具体，引用界面上的真实文字）",
  "evidence": "你依据哪些界面内容判断的",
  "event_id": "证据 ID，从上面给的 evidence_event_ids 里原样抄一个；没有就空字符串",
  "confidence": "high",
  "note": "补充说明"
}
```

字段规则：
- `passed`：**true / false / null**。用 `null` 表示"看不清、判不了" ——
  **不要猜**。猜 PASS 是编造，猜 FAIL 是冤枉产品，两种都比 null 糟。
- `actual`：必填，写屏幕上**真实看到的内容**，不要写"符合预期"这种空话。
- `confidence`：只能是 `high` / `medium` / `low` 之一。
- 其余字段没有就给空字符串，**不要省略键**。

## 判不了时，可以要求"补一步动作"

有些预期**光看这一屏根本判不了**，因为要看的界面还没打开。
例如「能调背景色，共 10 种」—— 屏幕只显示一行「课程背景色」，
色块面板没展开，数不出来。

这时候**不要**判 false（产品可能没问题，是我们没打开），
也**不要**只给个 null 就完事。请给 `need_action`，让执行器去补那一步：

```json
"need_action": {
  "verify_target": "要核验什么（一句话）",
  "why_insufficient": "为什么现在这一屏判不了（具体缺什么）",
  "related_elements": [
    {"rid": "llCourseColor", "text": "课程背景色"}
  ],
  "close_with": "取消"
}
```

- `related_elements` —— **从上面给的界面元素里挑**，不要自己编。
  这些是"执行器可能要去点的东西"。
- `close_with` —— **点开之后，看完怎么关回去**。
  像人类测试员那样：点开色板看完，点「取消」退回编辑弹窗，再继续下一步。
  这里填那个"关闭/取消/返回"按钮的**文字**（如 `取消`）。
  如果点开后是同一屏、不需要关，就填空字符串 `""`。
- 同时 `passed` 仍填 `null`（表示这轮判不了），`actual` 写清缺什么。
- 如果这一屏**已经足够判断**，或者再怎么点也看不到，就**不要**给
  `need_action`（给空对象 `{}`），直接判 true/false。

执行器补完动作会**拿新的界面回来再问你一次**（最多 3 次）。

## 输出格式硬要求

- 以 `{` 开头、`}` 结尾；不要 ```json 围栏，不要前言和后缀。
- 只输出一个 JSON 对象。
"""


def _fmt_clickable_name(item: Any) -> str:
    """把可点项排版成**只有语义名字**的一行 —— 不含坐标。

    例：
        `iv_more`
        `课程表`（菜单项，只有文字没有 rid）
        `cv_empty_content#1`（同名多个，带序号）

    ## 为什么要带序号

    176 的空课表网格有 25 个 FrameLayout 的 rid 全叫 cv_empty_content，
    只写名字模型没法说清要点第几个。序号是**执行器解析时用的**，
    和 `_resolve_strong_target` 的 `名字#n` 语法一一对应。
    """
    if not isinstance(item, dict):
        return str(item)
    rid = str(item.get("r") or item.get("rid") or "").strip()
    txt = str(item.get("t") or item.get("text") or "").strip()
    desc = str(item.get("d") or item.get("desc") or "").strip()

    idx = item.get("_ordinal")
    # 优先用文字（人能读懂），没文字才退回 rid
    name = txt or desc or rid
    if not name:
        return "(无名控件)"
    if rid and name != rid:
        label = f"{name}  (rid={rid})"
    else:
        label = name
    if idx:
        # ★ 序号用 rid 或文字都行，统一挂在后面
        label = f"{label}  → 要指明第几个时用：{rid or name}#{idx}"
    return label


def _fmt_clickable(item: Any) -> str:
    """把可点项排版成模型好读的一行。

    真机是 dict：{r:rid, t:text, d:desc, c:[x,y], ...}。
    必须把 **rid 和坐标都写清楚**，否则模型没法从列表里挑坐标。
    """
    if not isinstance(item, dict):
        return str(item)
    rid = item.get("r") or item.get("rid") or ""
    txt = item.get("t") or item.get("text") or ""
    desc = item.get("d") or item.get("desc") or ""
    c = item.get("c") or item.get("center") or []
    parts = []
    if rid:
        parts.append(f"rid={rid}")
    if txt:
        parts.append(f"text={txt!r}")
    # ⚠️ desc 和 text 经常是**同一个值**（工具层为兼容两种格式都填了）。
    #    早先无条件两个都输出，一屏几十个控件就是几十行重复文字 ——
    #    而这段每轮 prompt 都要带一遍，是上下文膨胀的稳定来源。
    #    重复的值对"挑坐标"没有任何信息量。
    if desc and desc != txt:
        parts.append(f"desc={desc!r}")
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        parts.append(f"c=[{c[0]},{c[1]}]")
    return " ".join(parts) if parts else str(item)


def _fmt_node(n: dict) -> str:
    """渲染一个语义节点（工具层 `summarize_xml` 的产物）。

    nav 模式的短键：i=下标 t=文字 r=rid末段 c=中心坐标 s=可滑动 off=不可用
    checked/selected=选中态。full 模式是完整键名，两种都要认 ——
    早先只认长键名，喂短键的节点会渲染成空行。
    """
    def g(*keys: Any) -> str:
        for k in keys:
            v = n.get(k)
            if v not in (None, "", False):
                return str(v)
        return ""

    bits = []
    rid = g("r", "rid")
    txt = g("t", "text", "d", "desc")
    if rid:
        bits.append(f"rid={rid}")
    if txt:
        # 文字是判断"页面上有什么"的关键，放前面
        bits.append(f"「{txt[:60]}」")
    c = n.get("c") or n.get("center")
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        bits.append(f"c=[{c[0]},{c[1]}]")
    if n.get("s") or n.get("scrollable"):
        bits.append("可滑动")
    if n.get("off") or n.get("disabled"):
        bits.append("不可用")
    # ⚠️ 三个键都要认：`checked`/`selected` 是工具层 summarize_xml 的原键名，
    #    `sel` 是 `_brief_nodes` 压缩后的短键。早先只认前两个，
    #    于是"这个开关是开着的吗"这种信息在渲染时被静默丢掉 ——
    #    模型看不到选中态，就会去猜、或者重复点开关来确认。
    if n.get("checked") or n.get("selected") or n.get("sel"):
        bits.append("已选中")
    return " ".join(bits) if bits else "（无文字节点）"


def _step_schema_doc() -> str:
    """输出格式的总说明 —— 以 `_SYSTEM` 里的 7 类契约为准。

    ⚠️ 这里**只列 kind 和必填字段**，不再重复描述各字段语义。
    早先这里有一份完整字段表（next_x/next_y/expect/note…），
    和 `_SYSTEM` 里的描述**两处并存**，模型要自己对两套说法，
    经常挑错键。现在单一事实来源就是 `_SYSTEM`。
    """
    return """每轮只输出一个 JSON 对象，`kind` 必填，取值只能是这 7 种之一：

  {"kind": "tap",       "target": "控件名/rid", "why": "..."}
  {"kind": "input",     "target": "输入框 rid", "text": "...", "why": "..."}
  {"kind": "key",       "key": "back|home", "why": "..."}
  {"kind": "done",      "why": "..."}
  {"kind": "knowledge", "query": "检索词", "why": "..."}
  {"kind": "vision",    "question": "要问的具体问题"}
  {"kind": "give_up",   "reason": "..."}

不要输出坐标。target 原样取自「当前可点项」。"""


def decide_step(python: str, skill_root: str, payload: dict[str, Any],
                knowledge_text: str = "", image: str | None = None,
                max_tokens: int = DEFAULT_MAX_TOKENS,
                timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """正常模式：判断当前步骤是否完成，以及下一步点哪。

    返回 StepAction 形状的 dict。坐标一定来自 payload["clickable"]。
    """
    lines: list[str] = []
    lines.append(f"用例：{payload.get('case_title') or payload.get('case_id')}")
    lines.append(f"当前步骤（第 {payload.get('step_index', 0) + 1}/"
                 f"{payload.get('step_total', '?')} 步）：{payload.get('step_desc')}")
    # ★ 代码没能直点 —— 这是模型这次被叫来的**唯一原因**，必须放最前面。
    #
    # 不说清的话模型会从零重新理解这一步：实测它跑去点
    # top_button_date_layout / left_button 这些跟本步无关的控件，
    # 在两点之间弹 6 轮才被死循环检出，白烧 60 秒。
    # 告诉它"代码本想找什么、为什么定不下来"，它才知道要解决什么问题。
    if payload.get("code_gave_up"):
        lines.append("")
        lines.append("🔁 代码没能直接定位（这就是叫你来要解决的问题）：")
        lines.append(f"   {payload['code_gave_up']}")
        lines.append("   ⇒ 可能是控件变了、页面变了、或前置界面没处理。")
        lines.append("   ⇒ 请朝着**本步的目的**推进（见「当前步骤」与"
                     "「本步目标」），不要乱点无关控件。")
    if payload.get("step_target"):
        lines.append(f"本步目标：{payload['step_target']}")
    if payload.get("step_landing"):
        lines.append(f"本步期望落点：{payload['step_landing']}")
    if payload.get("step_expect"):
        lines.append(f"期望结果：{payload['step_expect']}")
    # 坑位必须给模型看 —— 它们通常写着"到此为止，不要继续"这类边界，
    # 缺了它模型会"过度完成"（实测：s2 要求只验入口能进，
    # 模型没看到该约束，自己决定去选图，白点 9 次同一张照片）。
    pitfalls = payload.get("pitfalls") or []
    if pitfalls:
        lines.append("")
        lines.append("⚠️ 本步已知的坑与边界（务必遵守）：")
        for p in pitfalls:
            lines.append(f"  - {p}")
    lines.append("")
    lines.append(f"当前界面 activity：{payload.get('activity') or '（未知）'}")
    lines.append(f"已操作轮次：{payload.get('round', 0)}"
                 f"（上限 {payload.get('max_rounds', 40)}）")

    # ★★ 这条用例**最终要验证什么**。
    #
    # 早先完全不给 —— 模型只知道"这一步要走到哪"，不知道"走完要看什么"。
    # 于是执行时毫无方向感：该留意某个开关状态时没留意，该记下颜色名时没记，
    # 到最后断言阶段只能翻痕迹猜，甚至反过来重跑一遍。
    #
    # 只给 id + 一句话 + 判据原文，不给整份 YAML：
    # 目的是让它知道"往哪个方向看"，不是让它替代码判断（断言有独立的闸门）。
    asserts = payload.get("asserts_brief") or []
    if asserts:
        lines.append("")
        lines.append(f"本用例最终要验证的预期（共 {payload.get('asserts_total', len(asserts))} 条，"
                     f"执行时请留意与之相关的界面状态）：")
        for a in asserts:
            aid = a.get("id") or "?"
            desc = str(a.get("desc") or "").strip()
            chk = str(a.get("check") or "").strip()
            line = f"  · {aid} {desc}"
            if chk:
                line += f"（判据：{chk}）"
            lines.append(line)

    # ★★ 最近 3 步做过什么。
    #
    # 只带 3 条是刻意的：更早的历史对"下一步点哪"没有价值，只占 token。
    # 但**这 3 条必须有** —— 没有它模型会反复点同一个地方，
    # 直到代码的循环检测事后把它拦下来（那时已经白烧好几轮）。
    #
    # ⚠️⚠️ **这里绝不能把坐标当成"可点的东西"给出去。**
    #   实测（168，轮03）踩过：模型刚点过 (1795,1583) 关权限弹窗，
    #   下一屏这个坐标已经不存在了，但它**照着历史里的坐标又抄了一遍**，
    #   于是 `_validate_action` 判"坐标不在当前可点项里"，直接放弃整步 ——
    #   三行历史毁掉了三步执行。根因是"给了它用不上的数字"。
    #
    #   所以历史只表达**语义**（点过哪个控件、到了哪），
    #   坐标一律不写，并明确说明"要重新从当前列表里选"。
    recent = payload.get("recent_actions") or []
    if recent:
        lines.append("")
        lines.append("你刚才做过的事（仅供回忆；界面已经变了，"
                     "**不要照抄这里的坐标**，下一步的坐标必须重新从"
                     "下面的当前可点项里选）：")
        for r in recent:
            bits = []
            if r.get("at"):
                bits.append(f"[{r['at']}]")
            if r.get("key"):
                bits.append(f"按键 {r['key']}")
            if r.get("rid"):
                bits.append(f"点了 rid={r['rid']}")
            elif r.get("xy"):
                bits.append("点了某个位置")
            if r.get("text"):
                bits.append(f"「{str(r['text'])[:24]}」")
            if r.get("to"):
                bits.append(f"→ 到达 {r['to']}")
            if r.get("why"):
                bits.append(f"（当时理由：{r['why']}）")
            lines.append("  - " + " ".join(bits))
    # 兼容老字段（纯文本历史）；现在由 recent_actions 承担主要信息
    if payload.get("history"):
        lines.append("")
        lines.append("本步最近点过的位置（**不要重复点**）：")
        for h in payload["history"][-6:]:
            lines.append(f"  - {h}")
    if payload.get("loop_warning"):
        lines.append("")
        lines.append(f"🚫 {payload['loop_warning']}")

    # ★ 权限框没能自动处理 —— 工具层只点"安全"按钮，剩下的交给模型。
    if payload.get("perm_unmatched"):
        pu = payload["perm_unmatched"]
        lines.append("")
        lines.append("⚠️ 屏幕上有系统权限框，但**没有一个按钮能自动点**——需要你决定：")
        if pu.get("prompts"):
            lines.append(f"   它正在申请：{'；'.join(pu['prompts'])}")
        lines.append(f"   可见按钮：{pu.get('buttons')}")
        lines.append("   工具层故意不点这两类按钮：")
        lines.append("     · 「拒绝并不再询问」→ 会设 don't-ask-again，"
                     "让后续所有授权分支永远不再弹窗")
        lines.append("     · 「允许访问所选照片」→ 只是部分授权，语义与「允许」不同")
        lines.append("   请按**本用例要验的分支**选一个按钮点掉，"
                     "让流程能继续；选不了就 give_up 并说明。")

    # ★ 上一次给的坐标不合法，正在重试 —— 这句话必须**放在可点项之前**，
    #   紧跟错误描述，否则模型会忽略它继续用自己的旧坐标。
    if payload.get("retry_hint"):
        lines.append("")
        lines.append("🚫 上一次的回复有问题，请改正：")
        for ln in str(payload["retry_hint"]).splitlines():
            lines.append(f"   {ln}")

    clickable = payload.get("clickable") or []
    lines.append("")
    lines.append(f"当前可点项（{len(clickable)} 个）—— **直接说名字，不要给坐标**：")
    # ★ 不把坐标给模型（用户要求：执行器只负责"下一步做什么"，
    #   元素定位是观察层用代码干的活）。
    #
    # 以前每行都带 c=[x,y]，模型就会去算/抄坐标 —— 而它抄错一个数
    # 就点到别的地方，报告里还看不出是"选错了元素"还是"坐标算错了"。
    # 现在只给名字，坐标由 _resolve_strong_target 从观察结果解析：
    #   唯一命中 -> 点；不唯一 -> 报错要求用 `名字#序号`；找不到 -> 报错换目标。
    #
    # 重名项要提前编号：176 的空课表网格 25 个格子 rid 全叫
    # cv_empty_content，不给序号模型根本说不清要点第几个。
    _ord: dict[str, int] = {}
    for item in clickable:
        if isinstance(item, dict):
            _k = (str(item.get("r") or item.get("rid") or "").strip()
                  or str(item.get("t") or item.get("text") or "").strip())
            if _k:
                _ord[_k] = _ord.get(_k, 0) + 1
                item = dict(item)
                item["_ordinal"] = _ord[_k]
        lines.append(f"  {_fmt_clickable_name(item)}")


    # ★★ 当前页面的完整语义节点（"最新的页面 XML 信息"）。
    #
    # 为什么 `clickable` 不够：它只有**可点**的东西，而很多判据要看的
    # 恰恰是不可点的 —— 标题、提示语、「还未添加课程表」这类空态文案。
    # 只给可点项，模型就"看不见"屏幕上明明写着的字。
    #
    # 为什么不像老 SKILL 那样直接塞 XML 原文：一屏 XML 实测 6~7 千字符，
    # 每轮都带一遍就是纯浪费。工具层的 `summarize_xml` 已经把它压成
    # 语义节点列表（同一屏 6826 → 509 字符，省 93%），信息量足够做判断。
    #
    # ⚠️ 这里只渲染**当前屏**，不带历史屏 —— 历史用 recent_actions 表达。
    screen_nodes = payload.get("screen_nodes") or []
    if screen_nodes:
        shown = [n for n in screen_nodes if isinstance(n, dict)]
        lines.append("")
        lines.append(f"当前页面的语义节点（{len(shown)} 个，含不可点文字，"
                     f"用于确认页面上到底有什么）：")
        for n in shown[:40]:
            lines.append(f"  {_fmt_node(n)}")
        if len(shown) > 40:
            lines.append(f"  …另有 {len(shown) - 40} 个节点省略")

    if knowledge_text:
        lines.append("")
        lines.append("你之前查到的知识卡内容：")
        lines.append(knowledge_text)

    # ★★★ 你上一轮问的那张图，答案在这里 ★★★
    #
    # 167 空转 21 次的**真正病根**（比 max_tokens 更根本）：
    #   vision 节点算出了答案，但 decide 的提示词里从来没有它，
    #   模型只看得到自己问过什么，看不到答案 —— 于是以为没看到，再问一次。
    #
    # 注意措辞：明确说"这就是你问的那个问题的答案"，并**要求它直接据此
    # 行动**。只贴一段"视觉模型说……"的话，模型可能还会想再确认一次；
    # 明确禁掉重复提问，才能终止那个循环。
    _vis = str(payload.get("vision") or "").strip()
    if _vis:
        lines.append("")
        lines.append("【你上一轮问的问题，视觉模型已经看过了，答案如下】")
        lines.append(_vis[:1500])
        lines.append("⚠️ 这就是答案。**不要再问同一件事** —— 直接用这个信息"
                     "决定下一步。如果这个答案里确实没有你要的信息，"
                     "换别的手段（点一下试试、或按 back），而不是再问问过的内容。")

    lines.append("")
    lines.append("如果屏幕上有挡路的弹窗，先点掉它再继续。")
    lines.append("回答方式：**直接调用一个工具**（不要输出 JSON 文本）。"
                 "点控件用 tap；填必填输入框用 type_text；"
                 "做完了用 done；走不通用 give_up。")
    _km = _payload_norm = None
    if payload.get("retry_hint"):
        lines.append(f"注意：{str(payload['retry_hint'])[:200]}")
    return _step_via_tools(skill_root, payload, "\n".join(lines),
                           max_tokens=max_tokens, timeout=timeout,
                           image=image, mode="decide_step")


def _step_via_tools(skill_root: str, payload: dict[str, Any],
                    prompt_text: str, *, max_tokens: int, timeout: int,
                    image: str | None, mode: str) -> dict[str, Any]:
    """把提示词发给模型，取回它调用的**工具**，翻译成 action dict。

    两条路并存（有意为之）：
      ① 模型回了 tool_calls  -> 用 tools.tool_call_to_action() 翻译
      ② 模型没回工具而是回了 JSON 正文 -> 退回 _extract_json()

    ② 不是冗余：不同网关/模型对 tools 的支持度不一致，某些会把
    tool_calls 折叠成正文。留着这条路，换模型时不用改代码。

    校验一条都不放松：翻译出来的 input rid 照样要能在当前可点项里找到；
    tap 的语义目标由 act 节点用 _resolve_strong_target 解析，解析不到就报错。
    """
    from . import tools as _tools

    cfg = _load_config(skill_root)
    # trace 标签：fix_start 模式没有 step_index，别把 None+1 打出个 "第1步"
    _si = payload.get("step_index")
    _tag = (f"{mode} · 第{_si + 1}步 {payload.get('step_id') or ''}"
            if isinstance(_si, int) else
            f"{mode} · 轮{payload.get('round', 0)} "
            f"{payload.get('want_activity') or ''}")
    msg = _chat_msg(cfg, [{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": prompt_text}],
                    max_tokens=max_tokens, timeout=timeout,
                    tools=_tools.decide_tools(), image=image, trace=_tag)

    got = _tools.first_tool_call(msg)
    if got is None:
        # ② 退回 JSON 正文：必须是 7 类之一，没有 kind 就是格式错误。
        reply = str(msg.get("content") or "")
        action = _extract_json(reply)
        return action_from_kind(action)

    name, args = got
    try:
        action = _tools.tool_call_to_action(name, args)
    except ValueError as exc:
        # 未知工具名 = 格式错误，抛出去让 runner 走"可重试"那条路
        raise DecisionError(str(exc)) from exc

    # need_knowledge / ask_vision / give_up / done 不碰屏幕
    if name in ("need_knowledge", "ask_vision", "give_up", "done"):
        return action

    # ★ tap 走语义目标（tap_target），坐标由 act 节点解析 —— 这里没有坐标可校验。
    #   input 要校验 rid 确实在当前可点项里（不然就是模型编的输入框）。
    #   press_key 也过 _validate_action，那里校验 key 只能是 back/home。
    if name == "tap":
        if not str(action.get("tap_target") or "").strip():
            raise DecisionError("tap 没有给 target")
        return action
    return _validate_action(action, payload.get("clickable") or [])


def decide_fix_start(python: str, skill_root: str, payload: dict[str, Any],
                     knowledge_text: str = "", image: str | None = None,
                     max_tokens: int = DEFAULT_MAX_TOKENS,
                     timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """起点纠正模式：当前不在用例起点，想办法走回去。

    这条路径的存在本身就是用户的明确要求：
    "应该聪明。能够自己查文档回到点。这种不应该 Block。
     我要的像人类测试员一样。不可能说找不到就不测试了吧"
    """
    lines: list[str] = []
    lines.append("⚠️ 当前不在用例要求的起点上，**先想办法走回去**，不要开始测步骤。")
    lines.append(f"期望起点：{payload.get('want_activity')}")
    lines.append(f"当前 activity：{payload.get('activity') or '（未知）'}")
    lines.append(f"差异：{payload.get('start_mismatch')}")
    lines.append(f"已尝试纠正轮次：{payload.get('round', 0)}")

    clickable = payload.get("clickable") or []
    lines.append("")
    lines.append(f"当前可点项（{len(clickable)} 个）：")
    for item in clickable:
        lines.append(f"  {_fmt_clickable(item)}")

    if knowledge_text:
        lines.append("")
        lines.append("知识卡内容（可据此找正确入口）：")
        lines.append(knowledge_text)

    lines.append("")
    lines.append("回到起点后调用 done；实在回不去才 give_up。")
    lines.append("回答方式：**直接调用一个工具**（不要输出 JSON 文本）。")
    return _step_via_tools(skill_root, payload, "\n".join(lines),
                           max_tokens=max_tokens, timeout=timeout,
                           image=image, mode="decide_fix_start")


def assert_step(python: str, skill_root: str, payload: dict[str, Any],
                knowledge_text: str = "", image: str | None = None,
                max_tokens: int = ASSERT_MAX_TOKENS,
                timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """断言模式：期望结果满足了吗？

    ⚠️ 默认额度用 `ASSERT_MAX_TOKENS`（12000），**不是** `DEFAULT_MAX_TOKENS`。
    原因见 `ASSERT_MAX_TOKENS` 的注释 —— 断言比"选下一个控件"更吃推理，
    4000 会让正文被 reasoning 挤成空字符串，然后被记成"判定缺失"。
    """
    lines: list[str] = []
    lines.append(f"用例：{payload.get('case_title') or payload.get('case_id')}")
    lines.append(f"进度：已走完 {payload.get('steps_done', '?')}/"
                 f"{payload.get('step_total', '?')} 步")
    if payload.get("step_desc"):
        lines.append(f"最后执行的步骤：{payload['step_desc']}")
    if payload.get("step_landing"):
        lines.append(f"该步要求的落点：{payload['step_landing']}")
    lines.append(f"要验证的预期（{payload.get('assert_id')}）："
                 f"{payload.get('desc')}")
    if payload.get("assert_expect"):
        lines.append(f"判据原文：{payload['assert_expect']}")
        lines.append("判据类型说明：")
        lines.append("  · activity=...     -> 该页面**在本次执行过程中到达过**即成立。")
        lines.append("        ⚠️ 断言是流程走完后统一判的，那时很可能已经翻到下一页了 ——")
        lines.append("        **不要因为「现在不在这个页面」就判 FAIL**。")
        lines.append("  · node_present=... -> 该控件只要在**本次执行过程中出现过**就算成立，"
                     "不要求现在还停留在那个页面")
        lines.append("  · 其它文字判据      -> 按字面理解，看当前界面或走过的路径能否佐证")

    # ★★★ 「可以 XX / 可 XX」这一类措辞怎么判（用户要求，planer 硬规则同源）★★★
    #
    # 预期原文里大量出现「可以点击保存」「可以调整上课时间」「共 10 种」这类写法。
    # 这类话**本身不可观察** —— 屏幕上没有哪一处写着"这个可以调整"。
    # 以前没有这段话，模型只能两种错法：
    #   · 看到入口行`llCourseColor`就判 true（等于没验，验的是"有这行"）
    #   · 看不到内容就判 false（冤枉产品）
    #
    # 正确做法是**换成能看见的东西**：要么有可观察痕迹（行内的当前值），
    # 要么明确说"要动手才知道"→ 判 null 并写清楚缺什么，别瞎给。
    #
    # ⚠️ 这段只影响判定措辞，不赋予断言动作能力（执行器不做动作）。
    lines.append("")
    lines.append("★ 关于「可以点击 / 可调整 / 可进入 / 共 N 种」这类预期，怎么判：")
    lines.append("  · 这类话**本身不可观察** —— 界面上不会有哪处写着「这个可调整」。")
    lines.append("    → 不要因为「有这个入口/这一行」就判 true，那等于没验。")
    lines.append("  · 先找**可观察的痕迹**：行内的当前值、默认值、单位、按钮的启用态。")
    lines.append("    例：「课程时间」行显示「第1节」、「上课周数」行显示「第1-20周」")
    lines.append("        —— 这些**已经能证明该项可调**，可判 true。")
    lines.append("  · 若这一屏只有入口、要**点进去**才看得到内容（如「课程背景色」只有标题、")
    lines.append("    没展开色块），说明**现有证据不足以判定**：")
    lines.append("    → 判 passed=null，在 actual 里写「需要点开 X 才能看到」，")
    lines.append("      **不要**判 false（产品没错，是我们没点）。")
    lines.append("  · 带具体数量的（如「共 10 种」）：XML 里数到的个数**不能直接当结论**")
    lines.append("    （实测会被嵌套/装饰节点干扰）。数不全就判 null，别硬报数字。")

    # ★ 代码算好的硬事实优先于模型的"当前界面"直觉。
    # 实测踩到：a1 要 activity=.timetable.display.TimetableActivity，
    # 判的时候早已翻到系统图库，模型照"当前"判 FAIL —— 而日志显示确实到过。
    #
    # ⚠️ 但**复核轮**（code_verdict=False）不能给这句"应判 passed=false"：
    #    复核轮存在的意义恰恰是"代码没搜到，请你自己看"。若还在这儿
    #    命令它判 false，复核就变成让模型给代码背书，白花一轮。
    recheck = payload.get("code_verdict") is False
    if payload.get("activity_reached") is not None and not recheck:
        lines.append("")
        if payload["activity_reached"]:
            lines.append("★ 代码核对结果：判据要求的 activity "
                         "**在本次执行中确实到达过**（见上方路径）。")
            lines.append("  → 除非你有别的反证，这条应判 **passed=true**。")
        else:
            lines.append("★ 代码核对结果：判据要求的 activity "
                         "**在本次执行中从未出现过**。")
            lines.append("  → 这条应判 **passed=false**。")

    # ★ 复核轮专用说明：代码说"不成立"，但那只代表它没搜到。
    if recheck:
        lines.append("")
        lines.append("═" * 46)
        lines.append("★ 这是**复核轮**：代码没能在留痕里搜到这条判据的证据。")
        lines.append(f"  代码原话：{payload.get('code_why') or '（未提供）'}")
        # ★ 把上一轮自己的结论摆出来。
        #
        # 没有这段的话，"复核 3 次"就成了把同一个问题原样问 3 遍 ——
        # 模型每次都从零开始，极可能给出同样的答案，纯烧钱。
        # 摆出来它才知道自己上一轮说过什么、这次该不该改口。
        _prev = str(payload.get("prev_opinion") or "").strip()
        if _prev:
            lines.append(f"  ★ 你上一轮的判断是：「{_prev}」")
            lines.append("    请对照这次的界面和控件清单，重新核对一遍：")
            lines.append("    上一轮如果看漏了（比如判的时候已经翻页），这次就改判；")
            lines.append("    上一轮如果是对的，就维持原判 —— 但 actual 要写得更具体。")
        lines.append("  但这只说明**代码没找到**，不等于功能不对。常见的假失败原因：")
        lines.append("    · 文案被拆进多个控件 / 只有部分节点带 text")
        lines.append("    · 控件没进「可点列表」，于是没被记下来")
        lines.append("    · YAML 里的 rid 是旧版本，实际控件换了名字")
        lines.append("    · 断言是流程末尾统一判的，那是的屏幕早已翻页")
        lines.append("  请**自己对证据做判断**：看当前界面、看走过的路径、看控件清单。")
        lines.append("   · 看得出预期真的达成了 -> passed=true，并在 actual 里写清了什么")
        lines.append("   · 看得出确实没有     -> passed=false，写清实际是什么样")
        lines.append("   · 两样都看不清       -> passed=null（不要猜），note 里说明缺什么")
        lines.append("═" * 46)

    if payload.get("activity_seen"):
        lines.append(f"本次到达过的 activity：{', '.join(payload['activity_seen'][:12])}")
    if payload.get("seen_rids"):
        lines.append(f"本次执行中见过的控件：{', '.join(payload['seen_rids'][:30])}")
    lines.append("")
    # ★★★ 证据来源必须说清楚（用户的要求：断言只给 XML，不给图）★★★
    #
    # 以前这里直接印"当前界面上的文字/控件"，用的却是 payload["clickable"]
    # —— 那是**最后那一刻**的可点列表。实测（run 68）：a6 验的是
    # 「新建课程表」的完成按钮，拿到的却是「编辑课程」页那一屏，
    # 于是只能答"看不到完成按钮"，被记成 BLOCKED。
    #
    # 现在证据按断言所属的**步骤**取（nodes._evidence_for_assert），
    # 并把"这份证据是哪一步的"写在最前面 —— 模型得知道自己看的是哪一屏。
    _src = str(payload.get("evidence_source") or "").strip()
    if _src:
        lines.append(f"★ 以下证据来自：{_src}")
        if not payload.get("evidence_bound", True):
            lines.append("  ⚠️ 这条断言没有绑定具体步骤，只能拿整轮最后一屏来判。")
            lines.append("     如果这一屏明显不是该验的那个界面，请判 passed=null，")
            lines.append("     并在 actual 里写清「该看的是哪一屏」。**不要**拿这一屏硬判。")
    lines.append(f"该屏 activity：{payload.get('screen_activity') or payload.get('activity') or '（未知）'}")
    lines.append("该屏界面上的文字/控件（XML）：")
    _shown = 0
    for item in (payload.get("screen_nodes") or [])[:60]:
        if isinstance(item, dict):
            lines.append(f"  {_fmt_clickable(item)}")
            _shown += 1
    if not _shown:
        for item in (payload.get("screen_clickable") or [])[:40]:
            lines.append(f"  {_fmt_clickable(item)}")
            _shown += 1
    if not _shown:
        _txt = str(payload.get("screen_nodes_text") or "").strip()
        if _txt:
            lines.append(f"  {_txt[:2000]}")
        else:
            lines.append("  （这一步没有留下界面证据）")

    # ★★★ 视觉通道的结论（用户要求：颜色/形状/XML 判不了的交视觉模型）★★★
    #
    # 这里放的是**已经问过视觉模型、它亲眼看过的结论**，不是让判定模型
    # 自己去想象。所以语气上是"视觉员报告"，不是"请你猜"。
    # 还要明确一条：视觉说看不清时，判定就该判 null，不能拿它当反证。
    _vr = payload.get("vision_result") or {}
    if isinstance(_vr, dict) and _vr.get("tried"):
        lines.append("")
        lines.append("═" * 46)
        lines.append("★ 视觉核验员（已看过该步骤的截图，以下是它的报告）")
        lines.append(f"  为什么看图：{_vr.get('why') or '（未说明）'}")
        if not _vr.get("ok"):
            lines.append(f"  ⚠️ 视觉核验失败：{_vr.get('error') or '未知原因'}")
            lines.append("  → 这条**不能**因此判 false；看不清就判 null。")
        elif _vr.get("can_answer") is False:
            lines.append("  它说：**截图里看不到能回答该预期的内容**。")
            lines.append(f"  原话：{_vr.get('detail') or '（未给出）'}")
            lines.append("  → 这条应判 **passed=null**，并在 actual 里写明"
                         "「需要点开/展开后才能核验，现有截图看不到」。")
            lines.append("     **不要**判 false —— 产品不一定有问题，是我们没看到。")
        elif _vr.get("can_answer") is None:
            lines.append(f"  它给了非结构化回复：{_vr.get('detail') or '（空）'}")
            lines.append("  → 你可参考，但**不要**基于此硬判。")
        else:
            lines.append(f"  它答：{_vr.get('answer') or '（无结论）'}")
            lines.append(f"  看到的细节：{_vr.get('detail') or '（无）'}")
            lines.append("  → 结合下面的 XML 证据一起判；两者冲突时以视觉为准"
                         "（颜色/形状只有它看得见）。")
        lines.append("═" * 46)
    if payload.get("visited"):
        lines.append("")
        lines.append("走过的路径（最近几段）：")
        for v in payload["visited"][-8:]:
            lines.append(f"  {v}")
    if knowledge_text:
        lines.append("")
        lines.append("知识卡内容：")
        lines.append(knowledge_text)

    lines.append("")
    lines.append("只输出 JSON（字段一个都不能少，顺序无所谓）：")
    lines.append("""{
  "kind": "verdict",
  "passed": true,
  "actual": "实际看到了什么（必须具体，写界面上的真实内容）",
  "evidence": "你依据哪些界面内容判断的",
  "event_id": "",
  "confidence": "high",
  "note": "",
  "need_action": {}
}""")
    lines.append("")
    lines.append("字段规则：")
    lines.append("  · kind      —— 固定填字符串 verdict，不许改")
    lines.append("  · passed    —— true / false / null 三选一；null = 判不了，不要猜")
    lines.append("  · actual    —— 必填，写界面上真实看到的内容")
    lines.append("  · event_id  —— 从上面 evidence_event_ids 里原样抄一个；没有就空字符串")
    lines.append("  · confidence—— 只能是 high / medium / low")
    lines.append("  · need_action—— 证据不够、需要执行器补一步动作时才填，"
                 "否则给空对象 {}")

    # ★★★ 断言判定**不给图**（用户的要求）★★★
    #
    # 优先级是：XML 能判就用 XML；文字要靠 OCR 才认得出走 OCR；
    # 都不行才请视觉模型 —— 而视觉是**另一条通道**（vision.py），
    # 不是把最后那张截图捎带塞进判定请求里。
    #
    # 实测代价（run 68）：带图时每条断言 51.7 万字符、图占 99.3%，
    # 8 条断言的图 md5 完全一致，而它们要验的根本不是同一屏。
    reply = _chat(_load_config(skill_root),
                  [{"role": "system", "content": _SYSTEM_VERDICT},
                   {"role": "user", "content": "\n".join(lines)}],
                  max_tokens=max_tokens, timeout=timeout, image=None,
                  trace=f"assert · {payload.get('assert_id') or ''}")
    verdict = _extract_json(reply)
    if "passed" not in verdict:
        raise DecisionError(f"断言回复缺少 passed 字段：{verdict}")
    # AssertVerdict 要求 actual —— 模型没给就退回用 evidence，避免整轮崩掉
    # （第一次真跑就栽在这：模型按自然语义给了 evidence，schema 却要 actual）
    if not str(verdict.get("actual") or "").strip():
        verdict["actual"] = str(verdict.get("evidence") or "").strip() \
            or "（模型未给出具体现象）"
    # event_id 必须是会话里真实存在的整数 ID；模型没给或给错，
    # 就退回用最近一个（本轮的观测/动作事件），别让整条断言因为
    # 记账问题被判 FAIL —— 实测这样会把 3 条全 PASS 的用例错判成 FAIL。
    valid = [str(e) for e in (payload.get("evidence_event_ids") or [])]
    got = str(verdict.get("event_id") or "").strip()
    if valid and got not in valid:
        verdict["event_id"] = valid[-1]
        verdict["event_id_corrected"] = got or "(空)"
    return verdict


# ── 校验 ────────────────────────────────────────────────────────────────────

def _parse_coord(item: Any) -> tuple[int, int] | None:
    """从可点项里取坐标。

    ⚠️ 真机上传进来的是 **dict**（{r/rid/t/c/...}），不是字符串。
    早先只按 `rid@[x,y]` 的字符串写，第一轮 decide 就 AttributeError
    —— 单元测试用的是我随手拼的字符串，所以没照出这个差异。
    这里两种都收。
    """
    if isinstance(item, dict):
        c = item.get("c") or item.get("center")
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            try:
                return int(c[0]), int(c[1])
            except (TypeError, ValueError):
                return None
        return None
    if not isinstance(item, str):
        return None
    lo, hi = item.rfind("["), item.rfind("]")
    if lo == -1 or hi <= lo:
        return None
    try:
        x_str, y_str = item[lo + 1:hi].split(",")
        return int(x_str.strip()), int(y_str.strip())
    except (ValueError, AttributeError):
        return None


def _parse_rid(item: Any) -> tuple[str, str] | None:
    """从可点项里取 (占位, rid)。没有 rid 返回 None。

    返回二元组是为了和 _parse_coord 保持同样"返回元组或 None"的形状，
    调用处用 `c[1]` 取 rid 就行。

    dict 形态是主路径（真机传的就是 dict）：
        {'r': 'et_schedule_name', 't': '课程表名称（必填）', 'c': [...]}
    字符串形态是单测/日志里可能出现的紧凑写法：
        "rid=et_schedule_name text='…' c=[x,y]"
    """
    if isinstance(item, dict):
        rid = str(item.get("r") or item.get("rid") or "").strip()
        return (rid, rid) if rid else None
    if not isinstance(item, str):
        return None
    # 兼容 "rid=xxx ..." 与 "text='xxx' ..." 两种紧凑写法
    m = re.search(r"\brid=([^\s,]+)", item)
    if not m:
        return None
    rid = m.group(1).strip()
    return (rid, rid) if rid else None


def _validate_action(action: dict[str, Any],
                     clickable: list[str]) -> dict[str, Any]:
    """守住最关键的一条纪律：坐标必须来自当前可点项。

    这是 SKILL 里反复强调的坑——复用旧坐标会点到错的地方，
    而且因为界面已变，错误很难被发现。宁可报错也不放行。
    """
    out = {
        "step_done": bool(action.get("step_done")),
        "next_x": action.get("next_x"),
        "next_y": action.get("next_y"),
        # ★ 输入动作（tools 的 type_text / 老的 input_rid）。
        #   必须在这里透传，否则下面"既没坐标也没完成"那条会把它当格式错误。
        "input_rid": str(action.get("input_rid") or ""),
        "input_text": action.get("input_text"),
        # ★ 系统键（tools 的 press_key）
        "key": str(action.get("key") or ""),
        "why": str(action.get("why") or ""),
        "expect": str(action.get("expect") or ""),
        "need_knowledge": str(action.get("need_knowledge") or ""),
        "vision_question": str(action.get("vision_question") or ""),
        "give_up": bool(action.get("give_up")),
        "give_up_reason": str(action.get("give_up_reason") or ""),
        # 副作用声明：原样透传，由 act 节点记账进报告
        "grants_permission": bool(action.get("grants_permission")),
        "permission_name": str(action.get("permission_name") or ""),
        "permission_decision": str(action.get("permission_decision") or ""),
        "is_dialog": bool(action.get("is_dialog")),
        "dialog_text": str(action.get("dialog_text") or ""),
        "note": str(action.get("note") or ""),
    }
    if out["step_done"] or out["give_up"] or out["need_knowledge"] \
            or out["vision_question"]:
        return out  # 这些情况不需要坐标

    # ★ 输入动作：不需要坐标，但 **input_rid 必须在当前可点项里**。
    #
    # 和坐标校验同一条纪律：模型不能对着一个当前界面上不存在的控件写字。
    # 只是把"坐标必须在列表里"换成"rid 必须在列表里"。
    if out["input_rid"]:
        allowed_rids = {c[1] for item in clickable
                        if (c := _parse_rid(item))}
        if allowed_rids and out["input_rid"] not in allowed_rids:
            raise DecisionError(
                f"要输入的控件 {out['input_rid']} 不在当前可点项里"
                f"（现有：{', '.join(sorted(allowed_rids)) or '无'}）。"
                "请从列表里选一个真正的输入框。")
        return out

    # ★ 系统键：back/home 不需要坐标，白名单校验
    if out["key"]:
        if out["key"] not in ("back", "home"):
            raise DecisionError(f"不认识的系统键：{out['key']}")
        return out

    if out["next_x"] is None or out["next_y"] is None:
        raise DecisionError(f"既没说完成/放弃，也没给坐标：{action}")

    allowed = {c for item in clickable if (c := _parse_coord(item))}
    try:
        want = (int(out["next_x"]), int(out["next_y"]))
    except (TypeError, ValueError) as exc:
        raise DecisionError(f"坐标不是整数：{out['next_x']},{out['next_y']}") from exc

    # ★ 可点项为空时**必须拒绝**，不能放行。
    # 早先是 `if allowed and want not in allowed`，空列表时短路 → 编造的坐标
    # 一路通过。这正好是 SKILL 反复警告的"复用旧坐标"事故：
    # 界面没给出可点项，模型却报了个坐标，那个坐标只可能来自它自己的记忆。
    if not allowed:
        raise DecisionError(
            "本次响应没有可点项，却给了坐标 "
            f"({want[0]},{want[1]})——坐标只可能来自旧记忆，拒绝。"
            "这种情况应该先重新 observe，或判定 give_up。")

    if want not in allowed:
        raise DecisionError(
            f"坐标 {want} 不在当前可点项里（{len(allowed)} 个可选）。"
            "必须用当前响应给的坐标，不能沿用旧的。")
    return out
