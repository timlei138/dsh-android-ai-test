"""验证"第一步自动注入知识卡目录"——把"先读卡"从自觉变成代码。

背景：早先的实现完全靠模型自觉 need_knowledge 去要。
真机实测 168 整条用例跑完，knowledge 命中 **0 次** —— 纪律形同虚设。

后来改成第一步注入全文，但日历知识卡 34552 字符、注入上限 4000 ——
等于只给了前 12%，而且模型不知道后面还有 88%。实测 168 依然 knowledge=0：
它拿到的是一张残缺的卡，也没意识到可以查。

现在的契约：
  1. 第一步：注入**目录**（有哪些章节），模型据此知道"能查什么"
  2. 后续步骤：不再重复注入（否则每轮都塞，挤爆上下文）
  3. 没有声明 knowledge_files 时：不注入，保持原行为
  4. 模型自己查到的内容优先级更高（更新的、更对口）
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import android_agent.agent.nodes as ca

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail else ""))


from android_agent.cases.paths import skill_root  # noqa: E402
SKILL = skill_root()
CARD = "knowledge/com.zui.calendar.md"
CARD_SIZE = len((Path(SKILL) / CARD).read_text(encoding="utf-8"))


class Ctx:
    skill_root = SKILL
    device = None
    python = sys.executable


def main() -> int:
    ca._CTX = Ctx()  # 注入上下文（_ctx() 读它）
    ca._ctx = lambda: Ctx()

    task = {"knowledge_files": [CARD]}
    step = {"id": "s1", "desc": "打开课程表"}

    print("=== 1. 第一步应注入目录 ===")
    k0 = ca._step_knowledge({"step_index": 0, "action": {}}, task, step)
    check("第一步有知识内容", bool(k0.strip()), f"{len(k0)} 字符")
    check("带文件名", CARD in k0, k0[:40])
    check("是目录（有章节条目）", "- " in k0 and "L" in k0)
    check("明确告诉模型可以再查", "need_knowledge" in k0)
    # ★ 核心：目录必须**远小于**原文，否则跟整份注入没区别
    check(f"远小于原文（{len(k0)} vs {CARD_SIZE} 字符）",
          len(k0) < CARD_SIZE * 0.2,
          f"压缩到 {len(k0) * 100 // CARD_SIZE}%")
    check("目录里含关键章节名", "导航入口" in k0)

    print("\n=== 2. 后续步骤不再重复注入 ===")
    for idx in (1, 2, 3):
        k = ca._step_knowledge({"step_index": idx, "action": {}}, task, step)
        check(f"第 {idx + 1} 步不重复注入", not k.strip(), f"{len(k)} 字符")

    print("\n=== 3. 没声明 knowledge_files 时不注入 ===")
    k = ca._step_knowledge({"step_index": 0, "action": {}},
                           {"knowledge_files": []}, step)
    check("空列表 -> 不注入", not k.strip())

    print("\n=== 4. 模型自己查到的优先 ===")
    k = ca._step_knowledge(
        {"step_index": 0, "action": {"knowledge": "[card.md:7] 命中行"}},
        task, step)
    check("检索结果覆盖自动注入", k == "[card.md:7] 命中行", k[:40])

    print("\n" + "=" * 46)
    print(f"  全部通过（{len(PASS)} 项）" if not FAIL
          else f"  {len(FAIL)} 项失败: {FAIL}")
    print("=" * 46)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
