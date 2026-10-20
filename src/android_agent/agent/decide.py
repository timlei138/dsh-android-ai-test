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


def _chat(cfg: dict[str, str], messages: list[dict[str, Any]],
          max_tokens: int, timeout: int,
          image: str | None = None, _retry: int = 2) -> str:
    """发一次 chat/completions，返回正文。支持可选附图（多模态）。

    _retry 是**剩余重试次数**（不是 bool）：空正文时逐级退避。
    0 表示不再重试，直接抛错。
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
    _want_json_mode = "json" in _all_text

    _payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    # ★ 结构化输出约束（服务端强制，不是"请模型自觉"）。
    #
    # 用户的诉求：能不能要求子 Agent 严格返回 JSON。
    # 光在提示词里写"只输出 JSON"那是**请求**，模型可以不遵守 ——
    # 实测就踩到 reason 里带裸双引号、把整轮用例打成 BLOCKED。
    # DeepSeek 支持和 OpenAI 一致的 json_object 模式，由服务端保证
    # 产出是合法 JSON 字符串。
    #
    # 官方给的三个前提，本模块都已满足：
    #   1. prompt 含 "json" 字样 ✓（上面已校验）
    #   2. 给出期望的 JSON 形状示例 ✓（_step_schema_doc / assert 字段说明）
    #   3. max_tokens 给够，避免截断 ✓（4000，空正文重试时翻倍到 ≥8000）
    #
    # ⚠️ 官方同时写明：json_object 模式下**偶发返回空正文**。
    #    这和本模块已有的"空正文退避重试"是互补的，不冲突 ——
    #    空正文仍走 _retry 那条路，不会被误当成"解析失败"。
    if _want_json_mode:
        _payload["response_format"] = {"type": "json_object"}

    body = json.dumps(_payload).encode("utf-8")

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
        text = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise DecisionError(f"LLM 返回结构异常：{str(data)[:300]}") from exc

    if not text:
        usage = data.get("usage") or {}
        # 空正文 = reasoning 把额度吃光了。
        #
        # 实测：真机跑到第 7 轮偶发一次（completion=8000 全是 reasoning），
        # 单独复现 3 次却是 0/3 —— 是模型侧的**瞬时抖动**，不是 prompt 缺陷。
        #
        # 关键在于：这种抖动**不该让整条用例挂掉**。一次 OOM 式的空正文
        # 就把已经走了 7 轮的用例判死，代价完全不成比例。
        #
        # 所以退避策略要有**变化**，不能只是加倍：
        # temperature=0 下同 prompt 同额度，重试大概率还是空。
        #   第 1 次重试：加倍额度 + 明确催它直接给结论（压 reasoning）
        #   第 2 次重试：换成极简 prompt（去掉长上下文，只留问题和 JSON 形状）
        if _retry >= 1:
            hard = messages + [{
                "role": "user",
                "content": ("直接给出 JSON 结果，不要展开分析。"
                            "你现在只需要回答：点哪个坐标、以及是不是权限/弹窗按钮。"
                            "立即输出 JSON，不要任何解释。")}]
            # ⚠️ 额度必须**至少**翻倍且不低于 8000 —— 压 reasoning 的同时
            # 还得留够写完整 JSON 的空间。实测踩到：额度被 reasoning 吃光后
            # JSON 只写了一半（"next_y": 就断了），整条用例因此挂掉。
            return _chat(cfg, hard, max(max_tokens * 2, 8000), timeout,
                         image=image, _retry=_retry - 1)
        raise DecisionError(
            "LLM 返回空正文（通常是 max_tokens 被 reasoning 吃光）。"
            f"usage={usage}，当前 max_tokens={max_tokens}，建议调大。")
    return text


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


# ── 提示词 ──────────────────────────────────────────────────────────────────

_SYSTEM = """你是一个 Android 真机测试执行者，像人类测试员一样操作手机。

铁律：
1. **只能点给定的可点项**。坐标必须从"当前可点项"列表里选，绝不自己编造坐标。
   列表每行长这样：`rid=iv_more c=[1920,1314]` 或 `text='课程表' c=[2300,900]`。
   你要填的 next_x/next_y 就是 `c=[x,y]` 里的两个数。
   有些项只有 text、没有 rid（菜单项常见），还有的只有坐标——都能点。
2. 如果目标不在列表里，考虑：先滚动、先返回、先关掉挡路的弹窗。
3. 遇到规则外的弹窗（权限说明、引导页、更新提示等）——**自动点掉它再继续**，
   不要因为没见过的弹窗就停下。通常"同意/确定/继续"是正确选择。
4. 路径不对时不要放弃，先想办法走回去（返回键、重新进入口、查知识卡）。
5. **不要过度完成**。本步的目标就是"到达期望落点"。一旦到了（或做完了描述的事），
   立刻置 step_done=true，不要在目标页上继续点下去。
   如果本步的坑位写着"到此为止/不要继续"，那更是明确边界，必须停。
6. **不要重复点同一个坐标**。如果上一次点的位置再点一次还是同样的界面，
   说明这个动作没用，必须换目标（换控件、滚动、返回，或判定已完成）。
7. 只在**确实无路可走**时才给 give_up=true。

### 关于弹窗和权限（重要，会被记录进报告）
8. 遇到挡路的弹窗（含 App 自己的提示框、系统权限框），**直接点掉，不要停下来问**。
   同时**必须如实申报**（这些字段会原样进报告，供人审计授权行为）：
   - 点的按钮是**同意/允许/授权**性质（文案含 同意/允许/始终允许/仅本次/授权，
     或系统权限框的肯定按钮）→ `grants_permission=true`，
     并填 `permission_name`（如 存储/相机/位置）和 `permission_decision`（allow/deny/仅本次）。
     **注意**：App 自绘的"权限说明页"只要按钮是「同意」，同样要申报 ——
     对审计者来说"点了同意"这个事实最重要，不要纠结它算不算"系统"框。
   - 点的是**任何挡路弹窗**（含上面的情况）→ `is_dialog=true`，
     并填 `dialog_text`（弹窗上的主要文字）。
   - 「知道了」「确定」这类纯提示框：`is_dialog=true`，但 `grants_permission=false`。

只输出一个 JSON 对象，不要任何其它文字。

### 输出格式（服务端已强制 JSON，这里说明怎么填对）
- 以 `{` 开头、`}` 结尾，不要加 ```json 围栏，不要写任何前言/解释。
- 所有键名用双引号；字符串值用双引号。
- ⚠️ **字符串里如果要出现双引号，必须转义成 \\"**。
  实测踩过：`"reason": "看到 "照片" 了"` 这种裸引号会让整个 JSON 失效。
  想表达引用时更安全的做法是改用书名号「」，或直接避免内层引号。
- 不要在 JSON 后面追加说明文字。
"""


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
    return """{
  "step_done": true/false,      // 这一步的目标是否已经达成
  "next_x": 数字,                // 要点的 x 坐标（必须来自可点项列表）
  "next_y": 数字,                // 要点的 y 坐标
  "why": "一句话说明为什么点这里",
  "expect": "点完之后期望看到什么（activity=xxx 或 rid=xxx）",
  "need_knowledge": "想查知识卡就填检索词，否则空字符串",
  "vision_question": "需要看图才能判断就填问题，否则空字符串",
  "give_up": false,             // 只在确实无路可走时为 true
  "give_up_reason": "",
  "grants_permission": false,   // 点的是系统权限框才填 true
  "permission_name": "",        // 如 存储/相机/位置
  "permission_decision": "",    // allow / deny / 仅本次
  "is_dialog": false,           // 点的是挡路弹窗（含 App 自己的提示框）才 true
  "dialog_text": "",            // 弹窗上的主要文字
  "note": "给测试报告的补充说明"
}"""


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
    lines.append(f"当前可点项（{len(clickable)} 个，坐标只能从这里选）：")
    for item in clickable:
        lines.append(f"  {_fmt_clickable(item)}")

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

    lines.append("")
    lines.append("如果屏幕上有挡路的弹窗，先点掉它再继续。")
    lines.append("只输出 JSON，字段如下：")
    lines.append(_step_schema_doc())

    reply = _chat(_load_config(skill_root),
                  [{"role": "system", "content": _SYSTEM},
                   {"role": "user", "content": "\n".join(lines)}],
                  max_tokens=max_tokens, timeout=timeout, image=image)
    action = _extract_json(reply)
    return _validate_action(action, clickable)


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
    lines.append("回到起点后置 step_done=true。实在回不去才 give_up=true。")
    lines.append("只输出 JSON，字段如下：")
    lines.append(_step_schema_doc())

    reply = _chat(_load_config(skill_root),
                  [{"role": "system", "content": _SYSTEM},
                   {"role": "user", "content": "\n".join(lines)}],
                  max_tokens=max_tokens, timeout=timeout, image=image)
    action = _extract_json(reply)
    return _validate_action(action, clickable)


def assert_step(python: str, skill_root: str, payload: dict[str, Any],
                knowledge_text: str = "", image: str | None = None,
                max_tokens: int = DEFAULT_MAX_TOKENS,
                timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """断言模式：期望结果满足了吗？"""
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

    # ★ 代码算好的硬事实优先于模型的"当前界面"直觉。
    # 实测踩到：a1 要 activity=.timetable.display.TimetableActivity，
    # 判的时候早已翻到系统图库，模型照"当前"判 FAIL —— 而日志显示确实到过。
    if payload.get("activity_reached") is not None:
        lines.append("")
        if payload["activity_reached"]:
            lines.append("★ 代码核对结果：判据要求的 activity "
                         "**在本次执行中确实到达过**（见上方路径）。")
            lines.append("  → 除非你有别的反证，这条应判 **passed=true**。")
        else:
            lines.append("★ 代码核对结果：判据要求的 activity "
                         "**在本次执行中从未出现过**。")
            lines.append("  → 这条应判 **passed=false**。")
    if payload.get("activity_seen"):
        lines.append(f"本次到达过的 activity：{', '.join(payload['activity_seen'][:12])}")
    if payload.get("seen_rids"):
        lines.append(f"本次执行中见过的控件：{', '.join(payload['seen_rids'][:30])}")
    lines.append("")
    lines.append(f"当前 activity：{payload.get('activity') or '（未知）'}")
    lines.append("当前界面上的文字/控件：")
    for item in (payload.get("clickable") or [])[:40]:
        lines.append(f"  {_fmt_clickable(item)}")
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
    lines.append("只输出 JSON：")
    lines.append("""{
  "passed": true/false,        // 期望结果是否满足
  "actual": "实际看到了什么（必须具体，写界面上的真实内容）",
  "evidence": "你依据哪些界面内容判断的",
  "event_id": "证据 ID，必须从上面给的 evidence_event_ids 里原样抄一个；没有就给空字符串",
  "confidence": "high/medium/low",
  "note": "补充说明"
}""")

    reply = _chat(_load_config(skill_root),
                  [{"role": "system", "content": _SYSTEM},
                   {"role": "user", "content": "\n".join(lines)}],
                  max_tokens=max_tokens, timeout=timeout, image=image)
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
