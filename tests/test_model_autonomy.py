# -*- coding: utf-8 -*-
"""验证「YAML 不对时，模型能自己试一两次」这条能力真的通了。

用户原话：
  「我想要的是子Agent能够聪明。即使YAML不对能自己尝试一两次。
    而不是完全依赖YAML。毕竟不同的版本可能又差异。」

169 卡死的真正原因不是模型不努力，而是它**没有表达手段**：
  · 想"退出相机"，但 StepAction 只有 next_x/next_y，只能点像素
  · 可点列表里只有 flash_toggle / camera_switch / shutter / intent_back_button
  · 于是它反复点 intent_back_button（「查看」不是返回），13 轮不中

本测试锁住三件事：
  1. 模型能表达 back/home
  2. 这些动作**真的被路由到 act 去执行**（不能被当成空动作丢掉）
  3. 重复点同一目标时，喂回给模型的提示会**引导它换前提**，而不只是换坐标
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


import android_agent.agent.nodes as ca

print("=" * 70)
print("1) StepAction 必须有 key 字段（模型表达系统动作的唯一途径）")
print("=" * 70)
fields = ca.StepAction.model_fields
ck("StepAction 有 key 字段", "key" in fields)
desc = (fields["key"].description or "") if "key" in fields else ""
ck("key 的说明提到 back", "back" in desc)
ck("key 的说明提到 home", "home" in desc)

a = ca.StepAction(step_done=False, key="back", why="退出相机")
ck("能构造 key=back 的动作", a.key == "back", str(a.key))

print()
print("=" * 70)
print("2) ★ 路由：模型给的 key 必须去 act 执行")
print("=" * 70)
r = ca.route_after_decide({"action": {"step_done": False, "key": "back"}})
ck("★ 模型 key=back -> act", r == "act", r)
r2 = ca.route_after_decide({"action": {"step_done": False, "key": "home"}})
ck("模型 key=home -> act", r2 == "act", r2)
r3 = ca.route_after_decide({"action": {"step_done": False, "_key": "back"}})
ck("回放层 _key=back -> act", r3 == "act", r3)
r4 = ca.route_after_decide({"action": {"step_done": True}})
ck("step_done 仍去 check", r4 == "check", r4)
r5 = ca.route_after_decide({"action": {"step_done": False}})
ck("无动作仍去 check", r5 == "check", r5)

print()
print("=" * 70)
print("3) payload 要告诉模型：能用 back、自己是不是跑到别的 App 了")
print("=" * 70)
import inspect

# payload 的构建在 _base_decide_node 里（decide_node 只做知识/看图的
# 现场处理，主体仍走旧实现）。
src = inspect.getsource(ca._base_decide_node)
ck("payload 带 system_actions", "system_actions" in src)
ck("payload 带 app_package", "app_package" in src)
ck("payload 带 off_app 判定", "off_app" in src)
ck("question 里教了 key=back", 'key=\\"back\\"' in src or "key=\"back\"" in src)

print()
print("=" * 70)
print("4) ★ 重复点头时的提示要引导换前提，不能只说换坐标")
print("=" * 70)
import android_agent.agent.runner as sa

src2 = inspect.getsource(sa)
ck("提示里出现『换目标≠只换坐标』", "换目标≠只换坐标" in src2)
ck("提示里明确给出 key=\\\"back\\\" 这条路",
   'key=\\"back\\"' in src2 or 'key="back"' in src2)
ck("提示解释了『前提本身可能错』", "前提本身" in src2)

print()
print("=" * 70)
print("5) 卡死终止仍然有效（不能因为放宽自主性而丢掉护栏）")
print("=" * 70)
ck("仍保留『判定卡死』", "判定卡死" in src2)
ck("仍保留交替死循环检测", "交替死循环" in src2)
ck("卡死原因含『卡死』字样（child_agent 据此全局终止）",
   "判定为卡死（代码强制终止）" in src2)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
