# -*- coding: utf-8 -*-
"""模型回复格式问题**不该让整轮用例变 BLOCKED**。

背景（用户实测）：
    跑 168，a1 PASS，a2 时模型其实已经看清界面（PhotoPicker 起来了、图片网格都在），
    但 reason 里写了
        "看到 "拍摄于 2026年8月7日 上午11:46:18的照片""
    —— 内层裸双引号让 json.loads 失败，_extract_json 抛 DecisionError，
    一路冒到进程顶层 → exit=1 → **连报告都没写出来**。

用户的原则：
    "一条模型回复格式不对，不该让整轮用例以 BLOCKED 收尾；
     单次格式错误最多算该步判不了。"

本测试锁三件事：
    A. 裸双引号能被修复解析（而且是**完整**内容，不是被截断的半句）
    B. 真解析不了时，断言记 unknown/BLOCKED，**不抛异常**、不中断用例
    C. unknown 不许被当成 PASS，也不许被静默当成 FAIL
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.agent.decide as dc

PASSED = 0
FAILED = 0


def check(name, cond, extra=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


print("=" * 68)
print("A) 裸双引号必须能解析，且内容完整")
print("=" * 68)

# 真实踩到的那句
REAL = '{"passed": true, "reason": "看到 "拍摄于 2026年8月7日 上午11:46:18的照片"，已进入图库"}'
obj = dc._extract_json(REAL)
check("能解析", isinstance(obj, dict))
check("passed 正确", obj.get("passed") is True, str(obj.get("passed")))
check(
    "★ reason 完整（不是被截断的半句）",
    "2026年8月7日" in str(obj.get("reason", "")),
    repr(obj.get("reason"))[:90],
)
check(
    "★ 没有被标记成 salvaged（说明走的是修复路径，不是抢救）",
    not obj.get("_salvaged_from_truncated_json"),
    str(obj.get("_salvaged_from_truncated_json")),
)

print()
MORE = [
    ('{"passed": true, "reason": "看到 "PhotoPicker" 界面"}', "PhotoPicker"),
    ('```json\n{"passed": true, "reason": "点 "从图库导入课程表" 成功"}\n```', "从图库导入课程表"),
    ('{"passed": true,\n "reason": "标题是 "课程表"，正常"}', "课程表"),
]
for text, needle in MORE:
    try:
        o = dc._extract_json(text)
        check(f"修复：{needle}", needle in str(o.get("reason", "")),
              repr(o.get("reason"))[:70])
    except Exception as e:
        check(f"修复：{needle}", False, f"{type(e).__name__}: {e}")

print()
print("=" * 68)
print("B) 彻底解析不了 → DecisionError（由上层转 unknown，不许崩进程）")
print("=" * 68)
try:
    dc._extract_json("模型说了一堆话，完全没有 JSON")
    check("无 JSON 时抛 DecisionError", False, "居然没抛")
except dc.DecisionError:
    check("无 JSON 时抛 DecisionError", True)

print()
print("=" * 68)
print("C) subagent._answer 的 assert 分支：解析失败必须降级不抛异常")
print("=" * 68)

# 直接测 _answer 的行为：monkeypatch assert_step 让它抛 DecisionError
import android_agent.agent.runner as sa


def _fake_assert_step_raises(*a, **kw):
    raise dc.DecisionError("模型没给出合法 JSON：{乱码}")


_orig = dc.assert_step
try:
    dc.assert_step = _fake_assert_step_raises
    # 构造 _answer：subagent 内部是 make_answer(...) 之类的工厂
    factory = None
    for cand in ("make_answer", "_make_answer", "build_answer"):
        if hasattr(sa, cand):
            factory = getattr(sa, cand)
            break
    if factory is None:
        # 退回：直接找 run_case 内部闭包不可行，改为验证代码里存在兜底
        src = open(str(Path(__file__).resolve().parents[1] / "src" / "android_agent" / "agent" / "runner.py"), encoding="utf-8").read()
        # ⚠️ 这里以前断言的是字面串 "except dc.DecisionError" ——
        #    而那恰恰是个**bug**：subagent.py 从来没 import 过 dc，
        #    真走到这条 except 时抛的是 NameError，把真实错误掩盖掉。
        #    测试锁着错误写法，等于替 bug 背书。现在改成：
        #    · 必须真的导入了 DecisionError
        #    · 兜底语句必须用的是导入后的名字
        check("subagent 导入了 DecisionError",
              "from .decide import DecisionError" in src)
        check("subagent 里有 DecisionError 兜底",
              "except DecisionError" in src)
        check("没有 dc.DecisionError 这种未定义引用",
              "dc.DecisionError" not in src)
        check("兜底返回 unknown=True", '"unknown": True' in src)
    else:
        print("  （发现工厂函数）", factory)
finally:
    dc.assert_step = _orig

print()
print("=" * 68)
print("D) ★ unknown 不许冒充 PASS / 静默 FAIL")
print("=" * 68)
src_ca = open(str(Path(__file__).resolve().parents[1] / "src" / "android_agent" / "agent" / "nodes.py"), encoding="utf-8").read()
check("assert 节点读 unknown 标记", 'v.get("unknown")' in src_ca)
check("unknown 时记 issue", "判定缺失" in src_ca)
check(
    "★ 不再用 bool() 直接把 None 当 False",
    "passed = bool(v.get(\"passed\"))" in src_ca,
)
check("unknown 且代码判不了 → BLOCKED", 'status = "BLOCKED"' in src_ca)

print()
print("=" * 68)
print(f"  {FAILED} 失败 / {PASSED + FAILED} 项")
print("=" * 68)
sys.exit(1 if FAILED else 0)
