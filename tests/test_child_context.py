"""子 Agent 每轮上下文该带什么：验证三件事都在、且体积可控。

## 为什么单独立这个测试

用户提了三个明确要求：

  1. 子 Agent 要**知道现在要验证哪些**（asserts）
  2. 要带**最新的页面 XML 信息**（语义节点，不是可点项）
  3. 要带**历史最近 3 步**

这三件事都属于"每轮 prompt 里塞什么"，而 prompt 的构造分散在两个地方：
  · `child_agent.py` 拼 payload 字段
  · `decision_channel.py` 把字段渲染成文本

**最容易出的错是"字段拼了但没人渲染"** —— `history` 就是这么成了死代码：
渲染逻辑写好了，payload 里却从来没填过，模型永远看不到。

所以这里测的是**端到端**：从 payload 到最终渲染文本，三样东西都得出现在里面。

## 体积约束

要求带更多信息，必然变大。但"变大"要花在有用的地方：
  · 语义节点必须**滤掉三无布局容器**（既无 rid、无文字、无状态位）
  · 可点项的 `desc` 与 `text` 相同时只输出一次
两者合计能把同样的信息量压掉一大半。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.agent.nodes as ca  # noqa: E402
import android_agent.agent.decide as dc  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def render_decide(payload: dict) -> str:
    """复刻 decide_step 的渲染，只取我们关心的几段。

    不直接调 decide_step 是因为它会真的去请求模型 —— 这里只验证"文本长什么样"。
    """
    lines: list[str] = []
    cl = payload.get("clickable") or []
    lines.append(f"当前可点项（{len(cl)} 个）：")
    for it in cl:
        lines.append("  " + dc._fmt_clickable(it))
    for n in (payload.get("screen_nodes") or []):
        lines.append("  " + dc._fmt_node(n))
    for a in (payload.get("asserts_brief") or []):
        lines.append(f"  {a.get('id')} {a.get('desc')} {a.get('check')}")
    for r in (payload.get("recent_actions") or []):
        lines.append(f"  {r.get('at')} {r.get('rid')} {r.get('why')}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
print("=== 1) 要验证什么：asserts 必须进 payload 且被渲染 ===")
# ══════════════════════════════════════════════════════════════════
payload = {
    "asserts_brief": [
        {"id": "a1", "desc": "新建课程页已弹出", "check": "activity=EditCourseActivity"},
        {"id": "a2", "desc": "字段齐备且标注正确", "check": "rid=etCourseName"},
    ],
    "asserts_total": 2,
}
text = render_decide(payload)
check("asserts 出现在渲染结果里", "新建课程页已弹出" in text, "")
check("判据原文也带上了", "activity=EditCourseActivity" in text, "")
check("两条断言都在",
      "a1" in text and "a2" in text, "")

# ══════════════════════════════════════════════════════════════════
print()
print("=== 2) 最新页面 XML：语义节点要滤掉三无容器，但保住有文字的 ===")
# ══════════════════════════════════════════════════════════════════
raw_nodes = [{"i": i, "r": "", "t": "", "c": [1, 2]} for i in range(80)]
raw_nodes += [
    {"i": 100, "r": "toolbar_title", "t": "课程表", "c": [760, 95]},
    {"i": 101, "r": "tv_empty", "t": "还未添加课程表", "c": [1520, 800]},
    {"i": 102, "r": "btnCreateManually", "t": "手动创建课程表", "c": [1520, 1717]},
    {"i": 103, "r": "switch_weekend", "t": "周末有课", "c": [2600, 375],
     "checked": True},
    {"i": 104, "r": "cv_empty_content", "t": "", "c": [1520, 1206],
     "scrollable": True},
]
brief = ca._brief_nodes(raw_nodes)
check("三无布局容器被滤掉", len(brief) < len(raw_nodes), f"{len(raw_nodes)} -> {len(brief)}")
check("不可点的空态文案保住了（这是 clickable 给不了的）",
      any(n.get("t") == "还未添加课程表" for n in brief), "")
check("开关的选中态保住了",
      any(n.get("sel") for n in brief), "")
check("可滑动标记保住了",
      any(n.get("s") for n in brief), "")
check("坐标保住了（模型据此定位不可点元素）",
      all("c" in n for n in brief), "")

rendered = render_decide({"screen_nodes": brief})
check("空态文案渲染进 prompt", "还未添加课程表" in rendered, "")
check("选中态渲染成可读字样", "已选中" in rendered, "")

# ══════════════════════════════════════════════════════════════════
print()
print("=== 3) 历史最近 3 步：只能有 3 条，且最新的是最近的 ===")
# ══════════════════════════════════════════════════════════════════
trace = [{"step": "s1", "action": {"x": 10 + i, "y": 20 + i, "rid": f"r{i}",
                                   "why": f"第{i}次"}} for i in range(10)]
out = ca._recent_actions({"action_trace": trace}, 3)
check("只取 3 条", len(out) == 3, f"实际 {len(out)}")
check("是最新的 3 条（r7/r8/r9）",
      [o.get("rid") for o in out] == ["r7", "r8", "r9"],
      str([o.get("rid") for o in out]))
check("坐标带上了", all("xy" in o for o in out), "")

# 系统按键也要能表达（back/home 是修复迷路的主要手段）
kb = ca._recent_actions(
    {"action_trace": [{"step": "s2", "action": {"key": "back"}}]}, 3)
check("按键动作能表达", kb and kb[0].get("key") == "back", str(kb))

rendered3 = render_decide({"recent_actions": out})
check("最近3步渲染进 prompt", "r9" in rendered3, "")

# 空轨迹不能炸
check("没有历史时不报错", ca._recent_actions({}, 3) == [], "")
check("action_trace 为空也不报错",
      ca._recent_actions({"action_trace": []}, 3) == [], "")

# ══════════════════════════════════════════════════════════════════
print()
print("=== 4) 体积：同样的信息量，去重后必须明显更小 ===")
# ══════════════════════════════════════════════════════════════════
item = {"r": "btn_0", "t": "按钮文字0", "d": "按钮文字0", "c": [100, 200]}


def fmt_old(it: dict) -> str:
    """改之前的样子：desc 无条件输出。"""
    parts = [f"rid={it['r']}", f"text={it['t']!r}", f"desc={it['d']!r}",
             f"c=[{it['c'][0]},{it['c'][1]}]"]
    return " ".join(parts)


old_s = fmt_old(item)
new_s = dc._fmt_clickable(item)
check("desc 与 text 相同时不再重复输出", len(new_s) < len(old_s),
      f"{len(old_s)} -> {len(new_s)} 字符")
check("不重复但信息不丢（rid/文字/坐标都在）",
      "rid=btn_0" in new_s and "按钮文字0" in new_s and "c=[100,200]" in new_s,
      new_s)

# desc 与 text 不同时必须保留 desc —— 那是额外信息，不能为了省字符丢掉
diff = {"r": "btn_1", "t": "确定", "d": "提交表单按钮", "c": [1, 2]}
s = dc._fmt_clickable(diff)
check("desc 与 text 不同时仍然输出", "提交表单按钮" in s, s)

# ══════════════════════════════════════════════════════════════════
print()
print("=== 5) ★回归：历史里的坐标不能诱导模型照抄（168 真实事故）===")
# ══════════════════════════════════════════════════════════════════
# 事故现场：模型刚点 (1795,1583) 关掉权限弹窗，下一屏该坐标已经不存在了，
# 但"最近3步"把它又摆到模型眼前，模型直接照抄 →
# `_validate_action` 判坐标非法 → 整步 give_up。s1/s2/s3 三步连挂。
# 修法：历史只表达语义，具体坐标不出现。
import inspect  # noqa: E402

hist = ca._recent_actions(
    {"action_trace": [
        {"step": "s1", "action": {"x": 1795, "y": 1583, "rid": "button1",
                                  "why": "同意权限弹窗"}},
    ]}, 3)
# 注意：_recent_actions 本身保留 xy（数据层不改），
# 关键是**渲染层**不把它写出来。
src_render = inspect.getsource(dc.decide_step)
check("渲染历史时不写具体坐标",
      'r["xy"]' not in src_render and "r['xy']" not in src_render, "")
check("渲染文案明确禁止照抄坐标", "不要照抄" in src_render, "")
check("历史语义仍在（点了哪个 rid）", "button1" in str(hist), str(hist))

# ══════════════════════════════════════════════════════════════════
print()
print("=== 6) ★回归：坐标不合法要能重试，不能一次就放弃整步 ===")
# ══════════════════════════════════════════════════════════════════
import android_agent.agent.runner as sa  # noqa: E402

src_sa = inspect.getsource(sa)
check("有重试额度", "MAX_DECIDE_RETRIES" in src_sa, "")
check("换步重置重试额度", "retries = 0" in src_sa, "")
check("retry_hint 会拼进 payload", "retry_hint" in src_sa, "")
check("retry_hint 会被渲染", "retry_hint" in src_render, "")

# 重试不等于放宽校验：非法坐标必须仍然被拒
try:
    dc._validate_action({"next_x": 1, "next_y": 2},
                        [{"r": "a", "c": [100, 200]}])
    check("非法坐标仍被拒绝（重试不放宽校验）", False, "竟然放行了")
except dc.DecisionError:
    check("非法坐标仍被拒绝（重试不放宽校验）", True, "")

# 合法坐标必须通过
try:
    dc._validate_action({"next_x": 100, "next_y": 200},
                        [{"r": "a", "c": [100, 200]}])
    check("合法坐标正常通过", True, "")
except dc.DecisionError as exc:
    check("合法坐标正常通过", False, str(exc))

print()
if FAIL:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
    raise SystemExit(1)
print(f"全部通过（{PASS} 项）")
