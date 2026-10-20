"""回归：正文被 max_tokens 截断时，不能整条用例挂掉。

真机踩到（2026，轮02）：退避重试后模型输出
    {"step_done": false, "next_x": 337, "next_y":
就断了 —— JSON 不合法，_extract_json 抛错，已经跑了两轮的用例直接死。
但点击意图其实**已经清楚了**（step_done=false + next_x=337）。

契约：
  1. 截断但关键字段够用 → 抢救出 dict，标 _salvaged_from_truncated_json
  2. 缺 next_x/next_y 且 step_done=false → **不许猜**，照旧抛错
  3. 合法 JSON 不受影响
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import android_agent.agent.decide as dc

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def t(text, label):
    try:
        return dc._extract_json(text), None
    except dc.DecisionError as e:
        return None, str(e)


print("=== 1. 真机现场：截断在 next_y 后面 ===")
# 只有 next_x、没有 next_y —— **必须拒绝**。
# 点击需要两个坐标，拿 x=337 配一个猜出来的 y 去点，比直接报错危险得多
# （点到哪完全不可控）。所以这里的正确行为是抛错，不是抢救。
obj, err = t('{\n  "step_done": false,\n  "next_x": 337,\n  "next_y":', "trunc")
check("坐标不全 -> 拒绝（不猜 y）", obj is None and err is not None,
      (err or "居然放行了")[:60])

print("\n=== 1b. 两个坐标都在、只是后续字段被截断 -> 可以抢救 ===")
obj1b, err1b = t('{\n  "step_done": false,\n  "next_x": 337,\n'
                 '  "next_y": 203,\n  "why": "点更多', "trunc2")
check("抢救成功（不抛错）", obj1b is not None, err1b or "")
if obj1b:
    check("拿到完整坐标", (obj1b.get("next_x"), obj1b.get("next_y")) == (337, 203),
          str((obj1b.get("next_x"), obj1b.get("next_y"))))
    check("step_done 正确", obj1b.get("step_done") is False, str(obj1b.get("step_done")))
    check("标了抢救来源", obj1b.get("_salvaged_from_truncated_json") is True)

print("\n=== 2. 截断且缺坐标 -> 必须报错，不许猜 ===")
obj2, err2 = t('{\n  "step_done": false,\n  "why": "想点某个东西但坐标还没写"', "noc")
check("确实抛错（不瞎猜）", obj2 is None and err2 is not None, (err2 or "没报错")[:60])

print("\n=== 3. 截断但 step_done=true -> 可以抢救（本步完成不需要坐标）===")
obj3, err3 = t('{"step_done": true, "why": "到', "done")
check("step_done=true 可抢救", obj3 is not None and obj3.get("step_done") is True,
      err3 or str(obj3))

print("\n=== 4. 合法 JSON 不受影响 ===")
obj4, err4 = t('{"step_done": false, "next_x": 10, "next_y": 20, "why": "ok"}', "ok")
check("正常解析", obj4 is not None and obj4.get("next_x") == 10, err4 or "")
check("不打抢救标记", "_salvaged_from_truncated_json" not in (obj4 or {}))

print("\n=== 5. 围栏 JSON 仍能解析 ===")
obj5, _ = t('```json\n{"step_done": true, "why": "x"}\n```', "fence")
check("围栏内 JSON", obj5 is not None and obj5.get("step_done") is True, str(obj5))

print("\n=== 6. 退避重试的额度下限 ===")
src = Path(dc.__file__).read_text(encoding="utf-8")
check("翻倍后不低于 8000（留够写完整 JSON）",
      "max(max_tokens * 2, 8000)" in src)

print("\n" + "=" * 46)
print(f"  全部通过（{len(PASS)} 项）" if not FAIL
      else f"  {len(FAIL)} 项失败: {FAIL}")
print("=" * 46)
raise SystemExit(0 if not FAIL else 1)
