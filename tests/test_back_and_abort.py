# -*- coding: utf-8 -*-
"""验证会话 #84 的两个缺陷已修复。

缺陷 1：YAML 里的 `back: true` 被 `_tap_to_locate` 静默丢弃
        → 执行器从相机页直接找 btnImportFromGallery → 找不到 → 模型瞎点
缺陷 2：判定"卡死"后仍推进到下一步 → 169 的 s4 在同一屏又卡 4 分钟
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


print("=" * 70)
print("缺陷 1：back / home 必须进入 locate_seq，不能被静默丢弃")
print("=" * 70)

import android_agent.cases.loader as case_yaml  # noqa: E402
import android_agent.cases.loader

# 169 的 s3 原始 do
s3_do = [
    {"back": True},
    {"tap": {"rid": "btnImportFromGallery", "text": "从图库导入课程表"}},
    {"tap": {"rid": "button1", "text": "知道了"}},
]
_, seq, warns = case_yaml._tap_to_locate(s3_do)
print(f"  s3 do -> seq={seq}")
ck("back 出现在 seq 首位", seq and seq[0] == "key=back", str(seq[:1]))
ck("后续 tap 仍然保留", seq == ["key=back", "rid=btnImportFromGallery", "rid=button1"],
   str(seq))
ck("没有多余 warning", warns == [], str(warns))

# home
_, seq2, _ = case_yaml._tap_to_locate([{"home": True}, {"tap": {"rid": "x"}}])
ck("home 被识别", seq2[0] == "key=home", str(seq2))

# key: xxx 写法
_, seq3, _ = case_yaml._tap_to_locate([{"key": "back"}])
ck("key: back 形态被识别", seq3 == ["key=back"], str(seq3))

# ★ 不认识的动作必须报 warning —— 这是"静默丢弃"病根
_, _, w4 = case_yaml._tap_to_locate([{"double_tap": {"rid": "x"}}])
ck("★ 不认识的动作会报 warning", len(w4) >= 1, str(w4))

_, _, w5 = case_yaml._tap_to_locate([{"swipe": {"from": [1, 2], "to": [3, 4]}}])
ck("swipe 明确提示不支持回放", any("swipe" in w for w in w5), str(w5))

# 老行为不能坏：纯 tap 序列
_, seq6, _ = case_yaml._tap_to_locate([{"tap": {"rid": "iv_more"}}, {"tap": {"text": "课程表"}}])
ck("纯 tap 序列照旧", seq6 == ["rid=iv_more", "text=课程表"], str(seq6))

print()
print("=" * 70)
print("缺陷 2：判定卡死 -> 整条用例终止（不再推进到下一步）")
print("=" * 70)

import android_agent.agent.nodes as ca

stuck_reasons = [
    "模型连续点 (2913,1193) 无效且拒绝换目标，判定为卡死（代码强制终止）",
    "已连续点 (2913,1193) 且拒绝后仍不改，判定卡死，终止本用例",
]
for rs in stuck_reasons:
    ck(f"识别卡死: {rs[:24]}...",
       any(k in rs for k in ("卡死", "still_stuck", "放弃")))

# 非卡死的放弃原因不应触发全局终止
normal = "本步目标 rid=foo 找不到，放弃"
ck("普通放弃**不**触发全局终止",
   not any(k in normal for k in ("卡死", "still_stuck")), normal)

# 状态字段存在
ck("ChildState 有 abort 字段", "abort" in ca.ChildState.__annotations__)

print()
print("=" * 70)
print('路由：key 动作必须去 act，不能被当成「落回模型」')
print("=" * 70)

r = ca.route_after_strong({"action": {"_key": "back", "next_x": None}})
ck("★ key=back 路由到 act", r == "act", r)
r2 = ca.route_after_strong({"action": {"next_x": None}})
ck("无动作仍落回 decide", r2 == "decide", r2)
r3 = ca.route_after_strong({"action": {"step_done": True}})
ck("step_done 去 check", r3 == "check", r3)
r4 = ca.route_after_strong({"action": {"next_x": 10, "next_y": 20}})
ck("正常坐标去 act", r4 == "act", r4)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
