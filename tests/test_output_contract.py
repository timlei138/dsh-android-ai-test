"""钉住「分类 JSON 契约」—— 模型输出 7 类之一，每类固定格式。

## 为什么要钉

用户原话：「输出只说了一个输出一个JSON。格式也没说。很不容易解析。
            我理解应该分几类。并规定对应格式的JSON。」

以前的毛病：
  · `_SYSTEM` 只写"只输出一个 JSON 对象"，字段格式靠散落各处的自然语言描述
  · 模型靠猜，解析端只能一堆兜底（抠 JSON、修裸引号、抢救截断）
  · 出问题时报告里只有"模型回复无法解析（）"，无从下手

现在：`kind` 是唯一的判别键，7 类各有固定字段。这个测试把它锁死。

## 也钉住"模型不负责找元素"

`kind=tap` 只给**语义目标**（名字/rid），绝不给坐标。
坐标由 `_resolve_strong_target` 从观察结果解析。
这是用户明确的架构要求：
    观察(XML/OCR/视觉) -> 判断 -> 下一步 -> 观察
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from android_agent.agent.decide import (  # noqa: E402
    _KINDS, _SYSTEM, _SYSTEM_VERDICT, action_from_kind)

ok = 0
bad = 0


def ck(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        bad += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


print("=" * 70)
print("1. 7 类输出，每类都能翻译成 action")
print("=" * 70)

ck("恰好 7 类", len(_KINDS) == 7, str(_KINDS))
for k in ("tap", "input", "key", "done", "knowledge", "vision", "give_up"):
    ck(f"包含 {k}", k in _KINDS)

# tap：必须给语义目标，且**不带坐标**
r = action_from_kind({"kind": "tap", "target": "课程表", "why": "进课程表"})
ck("tap 带 tap_target", r and r.get("tap_target") == "课程表", str(r))
ck("★ tap 不输出坐标（模型不负责找元素）",
   r and "next_x" not in r and "next_y" not in r, str(r))
ck("tap 需要坐标时才由执行器解析", r and r.get("step_done") is False)

# input
r = action_from_kind({"kind": "input", "target": "et_schedule_name",
                      "text": "我的课程表", "why": "填名字"})
ck("input 翻译成 input_rid/input_text",
   r and r.get("input_rid") == "et_schedule_name"
   and r.get("input_text") == "我的课程表", str(r))
ck("input 空串是合法内容（清空框）",
   action_from_kind({"kind": "input", "target": "e", "text": ""})["input_text"] == "")

# key
r = action_from_kind({"kind": "key", "key": "back", "why": "退"})
ck("key 翻译成 key", r and r.get("key") == "back", str(r))

# done
r = action_from_kind({"kind": "done", "why": "到了"})
ck("done 翻译成 step_done", r and r.get("step_done") is True, str(r))

# knowledge
r = action_from_kind({"kind": "knowledge", "query": "课程表入口"})
ck("knowledge 翻译成 need_knowledge",
   r and r.get("need_knowledge") == "课程表入口", str(r))

# vision
r = action_from_kind({"kind": "vision", "question": "标题是啥"})
ck("vision 翻译成 vision_question",
   r and r.get("vision_question") == "标题是啥", str(r))

# give_up
r = action_from_kind({"kind": "give_up", "reason": "无路可走"})
ck("give_up 翻译成 give_up + 理由",
   r and r.get("give_up") is True and "无路可走" in r.get("give_up_reason", ""), str(r))


print()
print("=" * 70)
print("2. 格式错误要**明确报错**，不能静默")
print("=" * 70)

for bad_obj, why in [
    ({"kind": "walk", "why": "x"}, "不认识的 kind"),
    ({"kind": "tap", "why": "没给 target"}, "tap 缺 target"),
    ({"kind": "input", "text": "x"}, "input 缺 target"),
    ({"next_x": 1, "next_y": 2}, "没有 kind（旧坐标格式）"),
]:
    try:
        action_from_kind(bad_obj)
        ck(f"{why} -> 应报错", False, "没有抛异常")
    except Exception as exc:  # noqa: BLE001
        ck(f"{why} -> 报错", bool(str(exc).strip()), repr(exc))


print()
print("=" * 70)
print("3. 提示词里必须**逐类写清格式**")
print("=" * 70)

for k in _KINDS:
    ck(f"_SYSTEM 写了 {k} 的格式", f'"kind": "{k}"' in _SYSTEM
       or f'kind": "{k}' in _SYSTEM or f'`{k}`' in _SYSTEM)
ck("_SYSTEM 明确说不要输出坐标",
   "不要输出坐标" in _SYSTEM or "永远不要输出坐标" in _SYSTEM)
ck("_SYSTEM 要求单个 JSON、不要围栏",
   "```json 围栏" in _SYSTEM or "不要** ```json" in _SYSTEM or "不要 ```json" in _SYSTEM)
ck("★ 断言阶段用**另一套**契约（只判断不动作）",
   _SYSTEM_VERDICT != _SYSTEM and '"kind": "verdict"' in _SYSTEM_VERDICT)
ck("断言的 passed 支持 null（判不了不要猜）",
   "null" in _SYSTEM_VERDICT)


print()
print("=" * 70)
print(f"  {bad} 失败 / {ok + bad} 项")
print("=" * 70)
sys.exit(1 if bad else 0)
