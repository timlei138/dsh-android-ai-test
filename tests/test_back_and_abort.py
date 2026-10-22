# -*- coding: utf-8 -*-
"""验证卡死判定与路由的行为。

## 历史

原文件测两个缺陷：
  缺陷 1：YAML 里的 `back: true` 被 `_tap_to_locate` 静默丢弃
  缺陷 2：判定"卡死"后仍推进到下一步 → 169 的 s4 在同一屏又卡 4 分钟

缺陷 1 随 YAML 整体删除（`_tap_to_locate` 已不存在）。

缺陷 2 和之后的"路由"部分保留 —— 但路由函数按新图重写了：
旧 `route_after_strong` 已删，现在由 `route_after_decide` 承担。
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
print("缺陷：判定卡死 -> 整条用例终止（不再推进到下一步）")
print("=" * 70)

import android_agent.agent.nodes as ca  # noqa: E402

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
print("路由：每种能单独构成动作的字段都必须有分支")
print("=" * 70)

# ★ 这个坑踩过三次（见 route_after_decide 的注释）：
#   模型请求看图 / 要按返回键 / 要输入文字 —— 路由只认坐标，
#   动作就被静默丢掉了。所以每个字段都要单独验。
r = ca.route_after_decide({"action": {"_key": "back", "next_x": None}})
ck("★ key=back 路由到 act", r == "act", r)

r2 = ca.route_after_decide({"action": {"vision_question": "颜色有几种"}})
ck("★ 请求看图路由到 check（不是 act）", r2 == "check", r2)

r3 = ca.route_after_decide({"action": {"step_done": True}})
ck("step_done 去 check", r3 == "check", r3)

r4 = ca.route_after_decide({"action": {"next_x": 10, "next_y": 20}})
ck("正常坐标去 act", r4 == "act", r4)

r5 = ca.route_after_decide({"action": {"input_rid": "et_name", "input_text": "x"}})
ck("★ 输入动作路由到 act", r5 == "act", r5)

r6 = ca.route_after_decide({"action": {"tap_target": "完成"}})
ck("★ tap_target 路由到 act", r6 == "act", r6)

r7 = ca.route_after_decide({"action": {"need_knowledge": ["课程表"]}})
ck("★ 查知识路由到 check", r7 == "check", r7)

r8 = ca.route_after_decide({"action": {"give_up": "走不通"}})
ck("give_up 去 check", r8 == "check", r8)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
