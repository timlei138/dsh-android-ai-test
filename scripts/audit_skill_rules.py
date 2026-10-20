"""纪律 → 代码约束 的覆盖审计。

目标（用户原话）：
  "我想要的是把之前就 SKILLS 依赖模型自觉的都用代码的方式约束起来"

这份审计只回答一个问题：
  **每一条执行纪律，现在是"代码强制"还是"仍靠模型自觉"？**

三种状态：
  [C] code    —— 代码强制，违反会抛错/被拦，模型想不遵守都不行
  [P] prompt  —— 只写在提示词里，模型可以不听
  [G] graph   —— 由 LangGraph 骨架强制（不是模型能决定的）

用法：python scripts/audit_skill_rules.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# (纪律, 为什么需要, 当前状态, 落在哪)
RULES: list[tuple[str, str, str, str]] = [
    # ---- 已经用代码锁死的 ----
    ("坐标必须来自本次 observe/act 返回，不能复用旧坐标",
     "模型会照着上轮记忆里的坐标点，UI 变了就点空",
     "[C]", "decide._validate_action 抛 DecisionError"),
    ("可点项为空时不许编造坐标",
     "没得点却硬给坐标，等于盲点",
     "[C]", "decide._validate_action"),
    ("拿不到合法 JSON 不许乱点，必须响亮失败",
     "静默吞掉解析错误会变成「没反应」",
     "[C]", "decide._extract_json 抛 DecisionError"),
    ("rc==3（会话已终止）绝不重试",
     "终止后再操作会污染设备状态",
     "[C]", "runner 崩溃硬终止 + run_case 不重试"),
    ("只有 executed=False 才可安全重试",
     "executed 可能已点下去，重试会点两次",
     "[C]", "runner.start_check"),
    ("ENV 类前提（WiFi/飞行模式）必须提供 undo",
     "改了系统状态不恢复，后续用例全受影响",
     "[C]", "Precondition._env_needs_undo（Pydantic 校验）"),
    ("ENV 类前提无论成败都要恢复",
     "失败路径最容易漏恢复",
     "[C]", "run_restore 在 finish 无条件执行"),
    ("PASS/FAIL finding 必须绑定本会话证据 ID",
     "没有证据 ID 的结论无法复核",
     "[C]", "assert_verdict 校验 event_id ∈ 本会话"),
    ("判据只有一条：用例要求了吗（不做多余动作）",
     "多做动作会引入无关失败",
     "[C]", "prep 只执行用例声明的前提，不自行加动作"),
    ("失败要如实记录，不许粉饰",
     "结论被改写就看不出真问题",
     "[C]", "finding 的 actual 直接来自模型所写，代码不改写结论"),

    # ---- 仍靠模型自觉（这就是要补的） ----
    ("每步只有一个 act，然后从 next 找下一个目标",
     "多看多点是浪费轮次",
     "[P]", "写在 _SYSTEM，模型可以多观察/乱点"),
    ("第一反应是看 next，不要下意识再 observe",
     "重复 observe 没有新信息",
     "[P]", "没有代码统计 observe 次数"),
    ("不要过度完成（到达落点就停）",
     "多做一步可能改坏状态",
     "[P]", "坑位喂给模型了，但仍是建议"),
    ("滚动类控件绝对不要用 swipe（有惯性会过冲）",
     "过冲后要反向修正，越修越乱",
     "[P]", "模型选动作类型，代码不校验"),
    ("清空输入框用 --text=，不要连按退格",
     "退格次数算不准，留残字",
     "[P]", "完全没约束"),
    ("一批数据一次造完，再逐条操作",
     "来回切换页面成本高",
     "[P]", "没有约束"),
    ("重复手势用 --repeat，别拆成 N 次调用",
     "性能项，非正确性",
     "[P]", "没有约束"),
]


def main() -> int:
    print("=" * 68)
    print("  纪律 → 代码约束 覆盖审计")
    print("=" * 68)

    c = [r for r in RULES if r[2] == "[C]"]
    p = [r for r in RULES if r[2] == "[P]"]

    print(f"\n代码强制 [C]：{len(c)} 条")
    for name, why, _, where in c:
        print(f"  ✓ {name}")
        print(f"      {where}")
        print(f"      为什么：{why}")

    print(f"\n仍靠模型自觉 [P]：{len(p)} 条  ← 待补")
    for name, why, _, where in p:
        print(f"  · {name}")
        print(f"      {where}")
        print(f"      为什么：{why}")

    total = len(RULES)
    print(f"\n{'=' * 68}")
    print(f"  已锁死 {len(c)}/{total}（{len(c) * 100 // total}%）  "
          f"仍靠自觉 {len(p)}/{total}（{len(p) * 100 // total}%）")
    print("=" * 68)

    print("\n建议优先补的（影响结论正确性）：")
    for i, (name, _, _, _) in enumerate(p[:5], 1):
        print(f"  {i}) {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
