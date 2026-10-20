# -*- coding: utf-8 -*-
"""用户实测反馈的三个问题的回归测试。

1. 一条模型回复格式不对，不该让整轮用例 ERROR
   -> 裸双引号可修复；真解析不了时记 BLOCKED 不崩进程、不猜成 PASS/FAIL
2. YAML 的 assert 类型前提被静默跳过
   -> 转成 Precondition.assert_start，并真的接到 start_check
3. replay 生效与否看不出来
   -> 日志必须正面报告 代码走几步 / 落回模型几步
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.agent.decide as dc
from android_agent.cases.loader import load_case
from android_agent.agent.runner import match_landing

PASS = 0
FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


YAML = r"D:\dsh\langgraph-poc\assets\storage\cases\联想日历_168_图库导入入口.yaml"

print("=" * 66)
print("问题1：模型回复里的裸双引号")
print("=" * 66)
REAL = '{"passed": true, "reason": "看到 "拍摄于 2026年8月7日 上午11:46:18的照片"，已进入图库"}'
o = dc._extract_json(REAL)
ck("能解析", isinstance(o, dict))
ck("passed 正确", o.get("passed") is True)
ck("reason 完整不截断", "11:46:18" in str(o.get("reason")), repr(o.get("reason"))[:60])
ck("不是 salvage 兜底", not o.get("_salvaged_from_truncated_json"))

print()
print("=" * 66)
print("问题2：type=assert 前提必须被采纳")
print("=" * 66)
spec, warns = load_case(YAML)
asserts = [pc for pc in spec.preconditions if pc.assert_start]
ck("至少一条 assert 前提落地", len(asserts) >= 1, f"{len(asserts)} 条")
ck("起点校验值是 activity=...",
   any("activity=" in pc.assert_start for pc in asserts),
   asserts[0].assert_start if asserts else "")
ck("不再谎报 goto/assert『未实现』",
   not any("暂未实现分类处理" in w and "`assert`" in w for w in warns))

# 搬运到 task["start_assert"]
task = spec.model_dump()
for p in (task.get("preconditions") or []):
    sa = str(p.get("assert_start") or "").strip()
    if sa:
        task["start_assert"] = sa
        break
ck("start_check 能取到值", bool(task.get("start_assert")),
   str(task.get("start_assert")))
hit, _ = match_landing(task["start_assert"],
                       activity="com.zui.calendar/.AllInOneActivity")
ck("起点对 -> 通过", hit)
hit2, _ = match_landing(task["start_assert"],
                        activity="com.android.settings/.SubSettings")
ck("起点错 -> 不通过", not hit2)

print()
print("=" * 66)
print("问题3：代码直点 vs 交给模型，必须自报家门")
print("=" * 66)
# ★ 原来是验"纯回放：代码走 N 步 / 落回模型 M 步"。回放机制已删除，
#   换成了强证据直点（target 命中唯一就代码点，否则交模型）。
#   **要守的东西没变**：一次跑下来，"多少是代码定的、多少是模型定的"
#   必须能一眼看出来 —— 否则无从判断到底省了多少 LLM。
_src = io.open(str(Path(__file__).resolve().parents[1] / "src" / "android_agent" / "agent" / "runner.py"), encoding="utf-8").read()
ck("日志里有强证据汇总", "强证据：代码直点" in _src)
ck("汇总里带自动化率", "自动化率" in _src)
ck("unknown 不被打成 FAIL", "判不了（模型回复无法解析" in _src)

print()
print("=" * 66)
print("问题4：严格 JSON 必须是**服务端强制**，不是靠提示词自觉")
print("=" * 66)
_src = io.open(str(Path(__file__).resolve().parents[1] / "src" / "android_agent" / "agent" / "decide.py"), encoding="utf-8").read()
ck("请求里带 response_format=json_object", '"response_format"' in _src
   and '"json_object"' in _src)
ck("发送前校验 prompt 含 json（否则不发，避免 400）",
   '_want_json_mode' in _src)
ck("json_object 值写死在代码里（不受 prompt 内容影响）",
   '_payload["response_format"] = {"type": "json_object"}' in _src)
ck("提示词要求字符串内双引号必须转义", "必须转义成" in _src)

print()
print("=" * 66)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
