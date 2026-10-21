# -*- coding: utf-8 -*-
"""评审型 SKILL 检查：让一个"不知道代码"的读者照着卡走，看它会在哪卡住。

## 为什么静态检查不够

`check_skill_quality.py` 能抓"明显是考古/实现/重复"这类**形态问题**。
它抓不到语义问题：

  · 一句话说的是不是"照着做就能做"，还是需要额外背景才懂？
  · 两节的说法合起来，会不会推出一个矛盾的结论？
  · 某个关键情况（比如"代码没问我，但我看到明显不对"）卡里到底有没有交代？

这些只能靠**读**。而且最好由一个和真实执行者同族的模型来读 ——
它卡住的地方，就是真实执行模型会卡住的地方。

## 这个脚本做什么

它把技能卡（SKILL.md + references）拼成一份自包含的评审材料，
加上一段"你只能看到这份文档"的约束，产出可直接投喂给评审模型的 prompt。

    python scripts/review_skill.py            # 打印评审 prompt
    python scripts/review_skill.py --out f.md # 写到文件

然后把这个 prompt 交给一个**全新**的子 Agent（不要带任何代码上下文），
让它按 `REVIEW_QUESTIONS` 回答。它答不上来的题，就是卡的缺口。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SKILLS = Path.home() / ".agents" / "skills"
DIRS = ["android-case-runner", "android-case-planner"]

REVIEW_QUESTIONS = """
## 你的任务

你是一个第一次接触这个系统的测试工程师。**你只能看到下面这些文档，
看不到任何代码。** 请只依据文档，回答下面的问题。

回答要求：
- 每条都要**引用文档原文**作为依据。找不到依据就明确写"文档没写"。
- **不要猜**、不要假设"应该会这样"。你的价值恰恰在于暴露盲区。

### A. 可执行性（照着做，会不会中途卡住）

A1. 我要跑用例 168。从文档看，我第一步具体做什么？给到可以直接执行的程度。
A2. 跑到一半，我在一个陌生的页面上，找不到用例要求的东西。文档让我做什么？
    下一步动作具体是什么（点？按 back？按 home？放弃？）——文档有没有说清判断标准？
A3. 我判断"这一步完成了"，代码却说不对，还让我再来一次。文档有没有告诉我
    这时候该怀疑自己还是怀疑用例？重复几次之后怎么办？
A4. 弹出一个框。我怎么判断该不该处理它？文档给的标准够不够我当场做决定？

### B. 一致性（有没有互相打架的地方）

B1. 文档里有没有两处对同一件事的说法不一样？列出来。
B2. 有没有"文档 A 说要这样，文档 B 说要那样"的地方？

### C. 缺口（明显该说但没说的）

C1. 我遇到的最没把握、文档又没覆盖的情况是什么？
C2. 如果我是主 Agent（派任务的那个），看完这两份文档，
    我会不会派错 —— 比如把用例派给自己、或者要求子 Agent 改判据？
C3. 文档里有没有让你觉得"这是给写代码的人看的，不是给我看的"内容？

### D. 冗余（可以删的）

D1. 哪些段落你读完之后，**行为不会有任何变化**？（读了等于没读）
D2. 哪些内容在你已经知道结论的前提下，又花了大段解释"为什么"？
    这些"为什么"对**执行**有没有帮助？

### E. 最狠的一问

E1. 假设你只能保留这份文档的 **1/3**，你保留哪几节？为什么？
    删掉的部分里，有没有删了之后你会做错事的？
"""


def main() -> int:
    out_path = None
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        if i + 1 < len(sys.argv):
            out_path = Path(sys.argv[i + 1])

    parts = []
    total = 0
    for name in DIRS:
        d = SKILLS / name
        if not d.is_dir():
            print(f"⚠️  找不到 {d}", file=sys.stderr)
            continue
        for p in sorted(d.rglob("*.md")):
            rel = f"{name}/{p.relative_to(d).as_posix()}"
            txt = p.read_text(encoding="utf-8-sig")
            total += len(txt)
            parts.append(f"\n\n===== 文件：{rel} =====\n\n{txt}")

    if not parts:
        print("没有可评审的文档。", file=sys.stderr)
        return 1

    prompt = (
        "你正在评审一套 Agent 技能卡（skill）。\n"
        "这套卡的读者是两个 Agent：一个负责拆解用例并派发（planner），\n"
        "一个**没有 shell、只能用 `android_case` 工具**在真机上执行（runner）。\n"
        "下面是全部文档内容。"
        + "".join(parts)
        + "\n\n"
        + REVIEW_QUESTIONS
    )

    if out_path:
        out_path.write_text(prompt, encoding="utf-8")
        print(f"已写入 {out_path}（{len(prompt)} 字符，原文 {total} 字符）")
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
