"""知识卡模块：目录 / 按章节取 / 关键词检索。

## 这个模块为什么重要

子 Agent 的信息全靠知识卡 —— "这个 rid 找不到文案"、"滚轮要点准"、
"清数据会把备份一起删"。这些写在一份 3 万多字的卡里。

如果给的方式不对，等于没给：
  · 整份塞进去 → 上下文爆炸（3.4 万字符 ≈ 1.7 万 token/轮）
  · 截断前 4000 → 只给 12%，模型还不知道后面有 88%（实测 knowledge=0）
  · 只给关键词检索 → 模型得先猜对词，猜不对就抓瞎

所以这里的核心契约是：
  1. 目录必须**便宜**（压缩到原文 20% 以下），且列出所有可查的章节
  2. 按章节取能拿到**完整一节**，不截断（除非超长，且要明说）
  3. 找不到时要给**可用章节清单**，让模型自己纠错，而不是干等
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from android_agent.agent import knowledge as K  # noqa: E402
from android_agent.cases.paths import skill_root  # noqa: E402

SKILL = skill_root()
CARD = "knowledge/com.zui.calendar.md"
CARD_TEXT = (Path(SKILL) / CARD).read_text(encoding="utf-8")

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


print("=== 1) 章节切分 ===")
secs = K.parse_sections(CARD_TEXT)
check("切出多节", len(secs) > 30, f"{len(secs)} 节")
check("每节 range 合法（start <= end）",
      all(s.start <= s.end for s in secs))
check("行号从 1 开始", secs[0].start == 1, f"L{secs[0].start}")
# 父节必须包住子节 —— 否则"取一节"会漏掉它的细节
h2 = [s for s in secs if s.level == 2]
h3 = [s for s in secs if s.level == 3]
check("有二级和三级标题", h2 and h3, f"{len(h2)} 个 ## / {len(h3)} 个 ###")
if h2 and h3:
    parent = next((s for s in h2 if "备份与恢复" in s.title), None)
    child = next((s for s in h3 if "清数据会清掉备份" in s.title), None)
    check("父节包住子节", parent and child
          and parent.start <= child.start and child.end <= parent.end,
          f"父 L{parent.start}-{parent.end} / 子 L{child.start}-{child.end}"
          if parent and child else "没找到")

print("\n=== 2) 目录：必须便宜且完整 ===")
out = K.outline(SKILL, [CARD])
check("目录非空", bool(out.strip()), f"{len(out)} 字符")
check("标出文件名", CARD in out)
check("列出章节行数范围", "- " in out and "(L" in out)
# ★ 核心契约：目录要能放进首轮上下文，不能跟整份一样大
ratio = len(out) / len(CARD_TEXT)
check(f"压缩到原文 20% 以下（实际 {ratio * 100:.1f}%）", ratio < 0.2,
      f"{len(out)} / {len(CARD_TEXT)}")
# 目录不全就失去意义 —— 模型看不到的章节它不会去查
for title in ("导航入口", "坑-图库导入解析", "页面-课程时间设置页"):
    check(f"目录含「{title}」", title in out)

print("\n=== 3) 按章节取：要拿到完整一节 ===")
body = K.read_section(SKILL, CARD, "导航入口")
check("取到了", bool(body.strip()), f"{len(body)} 字符")
check("第一行就是标题", body.startswith("## 导航入口"), body.splitlines()[0][:30])
check("含实际内容（不是空壳）", "iv_more" in body, "有 iv_more")
# 「导航入口」下面是表格，没有子标题 —— 所以整节只有自己这一个 ## 行
check("不混进下一节的标题", body.count("## ") == 1,
      f"含 {body.count('## ')} 个 ## 行")

print("\n=== 4) 找不到章节要给可用清单（让模型能自己纠错）===")
miss = K.read_section(SKILL, CARD, "根本没有这个章节xyz")
check("明确说没找到", "没有标题含" in miss)
check("给出可用章节", "可用章节" in miss and "导航入口" in miss)

print("\n=== 5) best_section：短标题优先（更专指）===")
# "滚轮"同时出现在「弹框-日期/时间选择器」（大类）和「节行编辑弹窗（双滚轮）」里。
# 应该选短的、专指的那个 —— 长的那个整节有 300 多行，大半不相干。
best = K.best_section(SKILL, [CARD], "滚轮")
check("命中", bool(best))
if best:
    check("选的是专指那节（双滚轮）", "双滚轮" in best.splitlines()[0],
          best.splitlines()[0][:50])
    check("不会超长", len(best) < 3000, f"{len(best)} 字符")

check("没命中时返回空串（好让调用方退回关键词检索）",
      K.best_section(SKILL, [CARD], "完全不存在zzz") == "")

print("\n=== 6) 关键词检索 ===")
hits = K.search(SKILL, [CARD], "iv_more", limit=3)
check("检索有结果", hits and not hits.startswith("（"), hits.splitlines()[0][:60])
check("带出处（文件名:行号）", CARD in hits and ":" in hits)
check("达上限时明确提示", "上限" in hits)
check("无命中时明确说没命中",
      K.search(SKILL, [CARD], "zzz不存在zzz").startswith("（"))

print("\n=== 7) 边界情况 ===")
check("空关键词不崩", K.search(SKILL, [CARD], "") == "（关键词为空）")
check("文件不存在不崩", "不存在" in K.read_section(SKILL, "nope.md", "x"))
check("目录遇到不存在的文件也不崩",
      "不存在" in K.outline(SKILL, ["nope.md"]))
check("多文件目录能并列",
      K.outline(SKILL, [CARD, "knowledge/_system.md"]).count("[knowledge/") == 2)

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
