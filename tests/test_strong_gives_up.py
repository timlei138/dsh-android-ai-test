# -*- coding: utf-8 -*-
"""★ 核心机制测试：强证据直点**走不通时必须让模型介入**，而不是一直点。

## 用户原话（这是本测试要钉住的东西）

    「我想要的强证据知识为了减少 LLM 的调用。
      第一次的时候 YAML 应该能明白要写课程名才能保存。这应该是坑。
      那就需要增加个机制。如果强证据不能到预计那就让大模型介入。
      而不是一直无用的点。」

拆开来是四件事，逐条验：

  ① 强证据**存在**（能省 LLM 调用）—— 命中唯一时确实不问模型
  ② 强证据**有退出条件** —— 点了没进展就必须交出去
  ③ 交出去时**带上 YAML 的坑位说明** —— 模型不用重新踩一遍
  ④ **不许**一直无效地点 —— 有硬上限

## 176 的真实数据（本测试的依据）

    点击账本 seq 7~43：
        target = save_view「完成」(2913,202)
        before = .timetable.management.EditTimetableActivity
        after  = .timetable.management.EditTimetableActivity
        changed= false          ← 37 次，一次都没有进展

    根因：课程表名称是必填项，没填 -> 点「完成」被拦 -> 页面不动。
    YAML 的 s3.pitfalls 第一条**早就写了这件事**，但代码没机会把它交给模型，
    因为代码自己在原地打转，从没走到"叫模型"那一步。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import android_agent.agent.nodes as N  # noqa: E402
from android_agent.agent.nodes import (  # noqa: E402
    STRONG_MAX_SAME_TARGET,
    _ask_model,
    _resolve_strong_target,
    _strong_tries,
)
from android_agent.device.ledger import ClickLedger  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def ck(name, ok, detail=""):
    CHECKS.append((name, ok, detail))


# ── 上下文打桩：让 _strong_tries 读到我们造的账本 ────────────────
class FakeDev:
    def __init__(self, lg):
        self.ledger = lg


class FakeCtx:
    def __init__(self, dev):
        self.device = dev


_orig = N._ctx


def use_ledger(lg):
    N._ctx = lambda: FakeCtx(FakeDev(lg))


def restore():
    N._ctx = _orig


# ══════════════════════════════════════════════════════════════════
# ① 强证据本身要存在：命中唯一 -> 不问模型
# ══════════════════════════════════════════════════════════════════
print()
print("① 强证据能省 LLM 调用（命中唯一 -> 直接给坐标）")

xml = Path(tempfile.mkdtemp()) / "dump.xml"
xml.write_text(
    '<hierarchy rotation="0">'
    '<node text="完成" resource-id="com.x:id/save_view" class="android.widget.Button"'
    ' bounds="[1777,166][1877,239]" clickable="true" />'
    '<node text="" resource-id="com.x:id/other" class="android.widget.Button"'
    ' bounds="[10,10][100,100]" clickable="true" />'
    '</hierarchy>', encoding="utf-8")

empty_lg = ClickLedger(Path(tempfile.mkdtemp()) / "e.jsonl")
use_ledger(empty_lg)

st = {
    # ★ package / last_package 必须给：强证据只在被测 App 自己的界面里生效
    #   （用户：「只有本 APP 的才用强证据。跳出去都是让 AI 判断吧」）。
    #   不给包名就等于"归属不明"，新逻辑会正确地转交模型 ——
    #   那样这个用例就测不到"命中唯一 -> 直点"了。
    "task": {"package": "com.zui.calendar",
             "steps": [{"id": "s3", "desc": "点完成", "target": "rid=save_view",
                        "landing": "activity=.X"}]},
    "step_index": 0,
    "round_in_step": 5,
    "last_activity": ".timetable.management.EditTimetableActivity",
    "last_package": "com.zui.calendar",
    "obs_clickable": [{"r": "save_view", "t": "完成", "c": [1777, 202]},
                      {"r": "other", "t": "", "c": [50, 50]}],
    "obs_xml_path": str(xml),
}
# ⚠️ strong_target_node 已随旧图删除（15 -> 8 节点）。它做的事现在由
#    _resolve_strong_target 承担，plan/act 都调它 —— 所以直接测这个解析器。
x, y, why = _resolve_strong_target("rid=save_view", st)
ck("命中唯一 -> 给出坐标", x == 1777 and y == 202, f"({x},{y})")

x2, y2, why2 = _resolve_strong_target("text=不存在的按钮", st)
ck("找不到 -> 不给坐标", x2 is None, str(why2)[:60])

# ★ 两个同名控件时必须拒绝，不能"取第一个"糊弄
x3, y3, why3 = _resolve_strong_target(
    "text=完成",
    {**st, "obs_clickable": [{"r": "a", "t": "完成", "c": [10, 10]},
                              {"r": "b", "t": "完成", "c": [20, 20]}]})
ck("★ 命中多个 -> 拒绝（绝不取第一个）", x3 is None, str(why3)[:60])

# ══════════════════════════════════════════════════════════════════
# ② 点了 N 次没进展 -> 必须交出去
# ══════════════════════════════════════════════════════════════════
print()
print("② 点了没进展 -> 数得出来，该转交模型（这是 176 的解法）")

lg = ClickLedger(Path(tempfile.mkdtemp()) / "stuck.jsonl")
for _ in range(STRONG_MAX_SAME_TARGET):
    lg.record(step="s3", target={"rid": "save_view", "text": "完成"},
              before=".timetable.management.EditTimetableActivity",
              after=".timetable.management.EditTimetableActivity",
              changed=False)
use_ledger(lg)

# 没进展由 _strong_tries 计数决定，plan 据此转交模型。
n2 = _strong_tries(st, "s3", "rid=save_view")
ck("★ 没进展次数被数出来", n2 >= STRONG_MAX_SAME_TARGET, str(n2))
ck("★ 超过阈值 -> 该转交模型", n2 >= STRONG_MAX_SAME_TARGET)

# 第一次点的时候不该移交（否则强证据就没意义了）
lg1 = ClickLedger(Path(tempfile.mkdtemp()) / "one.jsonl")
for _ in range(STRONG_MAX_SAME_TARGET - 1):
    lg1.record(step="s3", target={"rid": "save_view"}, changed=False)
use_ledger(lg1)
n1b = _strong_tries(st, "s3", "rid=save_view")
ck("只点了 1 次 -> 还在阈值内，继续直点", n1b < STRONG_MAX_SAME_TARGET, str(n1b))

# ══════════════════════════════════════════════════════════════════
# ③ 交出去时，必须带上 YAML 里的坑
# ══════════════════════════════════════════════════════════════════
print()
print("③ 转交模型时带上 YAML 坑位（模型不用重新踩一遍）")

st_pit = dict(st)
st_pit["task"] = {"package": "com.zui.calendar", "steps": [{
    "id": "s3",
    "desc": "先点名称输入框输入课程名，再点完成",
    "target": "rid=save_view",
    "landing": "activity=.timetable.display.TimetableActivity",
    "pitfalls": [
        "★ 名称是**必填项**。不填就点完成，本 ROM 会弹起键盘把焦点抢到名称框、页面不动（已实测）",
        "输入框里显示的是占位符「课程表名称（必填）」，不代表已经有内容",
    ],
}]}
ans = _ask_model(st_pit, "点了 3 次界面都没变化")
guid = ans.get("fallback_guidance") or ""
ck("★ 指导语里带「上次踩过的坑」", "上次踩过的坑" in guid)
ck("★ 坑的内容真的传下去了（必填项）", "必填项" in guid, guid[-90:])
ck("★ 带上了这一步要做什么", "先点名称输入框" in guid)
ck("★ 带上了做完应该是什么", "TimetableActivity" in guid)
ck("★ 带上了代码为什么交给它", "点了 3 次界面都没变化" in guid)

# ══════════════════════════════════════════════════════════════════
# ④ 不许一直无效地点：有硬上限
# ══════════════════════════════════════════════════════════════════
print()
print("④ 硬上限：连续无效点击次数有天花板")

ck("★ 阈值很小（不会跑到几十次）", STRONG_MAX_SAME_TARGET <= 3,
   f"STRONG_MAX_SAME_TARGET={STRONG_MAX_SAME_TARGET}")

# 连续 10 次无效 -> 计数封顶后仍然判为"该交出去"
lg10 = ClickLedger(Path(tempfile.mkdtemp()) / "ten.jsonl")
for _ in range(10):
    lg10.record(step="s3", target={"rid": "save_view"}, changed=False)
use_ledger(lg10)
n10 = _strong_tries({}, "s3", "rid=save_view")
ck("★ 连续 10 次无效能被数出来", n10 == 10, str(n10))
ck("★ 10 次 > 阈值 -> 一定会交出",
   n10 >= STRONG_MAX_SAME_TARGET)

# 有进展就归零 —— 不能把"正常多步"误判成"卡住"
lgm = ClickLedger(Path(tempfile.mkdtemp()) / "mix.jsonl")
lgm.record(step="s3", target={"rid": "save_view"}, changed=False)
lgm.record(step="s3", target={"rid": "save_view"}, changed=True)   # 有进展
lgm.record(step="s3", target={"rid": "save_view"}, changed=False)
use_ledger(lgm)
nm = _strong_tries({}, "s3", "rid=save_view")
ck("★ 中间有进展 -> 从那次重新计数", nm == 1, str(nm))

# ══════════════════════════════════════════════════════════════════
# ⑤ 账本 changed 的语义：有进展，不只看跳没跳页
# ══════════════════════════════════════════════════════════════════
print()
print("⑤ 「有进展」的判据（不只看 activity 变没变）")

dev_src = (ROOT / "src/android_agent/device/direct.py").read_text(encoding="utf-8")
ck("★ 账本同时看 activity 和控件清单",
   "_act_before != scr.activity or _sig_before != _sig_after" in dev_src)
ck("★ 有 _clickable_sig 生成控件指纹", "def _clickable_sig" in dev_src)
ck("★ 指纹记进账本（事后能对）", '"sig": _sig_after' in dev_src)

restore()

# ══════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════
bad = [c for c in CHECKS if not c[1]]
print()
print("=" * 70)
for name, ok, detail in CHECKS:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail else ""))
print()
print(f"  {len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
print("=" * 70)
sys.exit(1 if bad else 0)
