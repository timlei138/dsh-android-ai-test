# -*- coding: utf-8 -*-
"""SKILL 文档质量检查（精简版，2026-10-27）。

## 保留什么

B 段：**技能卡不许出现被测 App 的专有内容**（包名 / 控件 id / 业务名词）。

用户原话：
「SKILL 应该包含的是高信息密度及共性的说明。
  不能包含和我们测试某个 APP 相关的内容」

这条价值很高 —— 它抓过我两次（我在示例里写了「课程表」）。

2026-11 增补 —— 用户原话：

  「dir "D:\\Project\\deepseek\\android-ai-test-skills 这种不应该写死路径」

→ 新增 C 段：不许写死个人机器路径 / 真用户名 / 真设备 serial；
  位置一律 `<target>`（工具层根目录）、`<serial>` 占位。

## 删掉什么

A 段：原来检查"文档提到的字段/机制，代码里真的有"。
它检查的是 `android-case-runner` 技能卡里的参数名、`landing` 判据、
`preconditions` 对象……那些全部随 LangGraph 方案一起废弃了。

`android-case-check` 现在**不涉及任何代码字段** ——
它讲的是"怎么判断用例够不够格"，是纯方法论，
所以没有"文档与代码不一致"的风险面。

## 关于内置 skill 的例外

`android-ai-test-skills`（老项目的 SKILL.md）**故意不在检查范围**：
它自带了工具层的使用示例，那些示例里出现 App 名是合理的
（它服务的是执行子 Agent，需要具体例子）。
本测试只管我们自己写的那份 check。
"""
import re
import sys
from pathlib import Path

SKILLS = Path.home() / ".agents" / "skills"
DOCS = [SKILLS / "android-case-check" / "SKILL.md"]

ok = 0
bad = 0
fails: list[str] = []


def ck(name: str, cond: bool, extra: str = "") -> None:
    global ok, bad
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        bad += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))
        fails.append(name)


print("=" * 78)
print("0) 文档都在吗")
print("=" * 78)
docs_text: dict[Path, str] = {}
for p in DOCS:
    exists = p.is_file()
    ck(f"存在：{p.relative_to(SKILLS)}", exists, str(p))
    if exists:
        docs_text[p] = p.read_text(encoding="utf-8")

print()
print("=" * 78)
print("A) 不含 App 专有内容（包名 / 控件 id / 具体业务名词）")
print("=" * 78)

APP_PATTERNS = [
    (r"com\.zui\.", "com.zui.* 包名"),
    (r"com\.android\.settings\b", "具体系统包名"),
    (r"\biv_more\b", "具体控件 id iv_more"),
    (r"\bitem_title\b", "具体控件 id item_title"),
    (r"\bbtnImportFromGallery\b", "具体控件 id"),
    (r"\btimetable\w*", "具体页面/控件名 timetable*"),
    (r"\balmanac\w*", "具体页面/控件名 almanac*"),
    (r"联想日历|课程表|黄历|纵览", "具体 App 的业务名词"),
    # ⚠️ 「日历」单独一条 —— 它太常见，之前混在别的词里漏掉了。
    #    SKILL 是通用规则，出现具体 App 名会让人以为只适用于那个 App。
    #
    #    例外：**Android 权限类型名**里的「日历」要放过 ——
    #    `CALENDAR` 权限的中文就叫"日历"，那是系统概念，不是被测 App。
    (r"(?<!通知、)日历", "具体 App 名「日历」"),
]
for pat, label in APP_PATTERNS:
    hits = []
    for p, t in docs_text.items():
        for i, ln in enumerate(t.splitlines(), 1):
            if re.search(pat, ln, re.I):
                hits.append(f"{p.name}:{i} {ln.strip()[:60]}")
    ck(f"没有「{label}」", not hits, str(hits[:3]))

print()
print("=" * 78)
print("B) 派发目标的名字写对了")
print("=" * 78)
# check skill 里提到要用哪个 preset 派发 —— 写错会派不出去
all_text = "\n".join(docs_text.values())
ck("提到 android-executor", "android-executor" in all_text,
   "派发目标写错的话 subagent 会被 allowedAgentPresets 拒绝")
ck("不再提 android-worker（已废弃）",
   "android-worker" not in all_text)

print()
print("=" * 78)
print("C) 不含写死的个人机器路径 / 真用户名 / 真设备 serial")
print("=" * 78)

PATH_PATTERNS = [
    (r"D:\\Project", "写死的作者机器路径 D:\\Project\\…"),
    (r"D:\\dsh\\", "写死的作者机器路径 D:\\dsh\\…"),
    (r"/home/xuyl", "写死的部署机路径 /home/xuyl/…"),
    (r"[Ll]eichao4|[Zz]hukl2", "真实用户名"),
    # 设备 serial 是 8 位小写 hex；边界排除 UUID 段（前后带 -）
    (r"(?<![0-9a-fA-F-])[0-9a-f]{8}(?![0-9a-fA-F-])", "疑似真设备 serial（该用 <serial> 占位）"),
]
for pat, label in PATH_PATTERNS:
    hits = []
    for p, txt in docs_text.items():
        for i, ln in enumerate(txt.splitlines(), 1):
            if re.search(pat, ln):
                hits.append(f"{p.name}:{i} {ln.strip()[:60]}")
    ck(f"没有「{label}」", not hits, str(hits[:3]))

print()
print("=" * 78)
if fails:
    print(f"  {bad} 处不一致")
    for f in fails:
        print("   ✗", f)
else:
    print(f"  全部一致（{ok} 项）")
print("=" * 78)
sys.exit(1 if bad else 0)
