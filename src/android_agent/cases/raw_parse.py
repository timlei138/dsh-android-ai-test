"""把用户手写的原始用例 txt 拆成「前提 / 步骤 / 预期」三段。

## 为什么要拆

用户的原话是：「记录详情。显示原始的步骤和期待，不是 YAML。显示是反的。」

原文长这样（176）：

    测试用例 联想日历_176
    前提：
    确保设备没有课程表。可以清空APP数据
    操作步骤
    1. 日历点击页面右上角更多按钮，选择课程表
    2. 点击 "手动创建课程表" 按钮
    ...
    预期结果
    1. 打开课程表页面
    ...

整段铺出来能看，但"第 3 步对应的预期是第 3 条"这层对应关系全靠人眼数。
拆成两列并排，一眼就能对上 —— 这才是看用例的人真正要的。

## 为什么用正则而不是解析器

原文是**人写的中文文本**，没有 schema，而且每份格式都不完全一样
（有的写「操作步骤」，有的写「测试步骤」，有的编号写「1.」有的写「1、」，
有的预期条目跨多行，还有的用 `·` 起子项）。

所以这里的目标不是"严格解析"，而是**尽力而为 + 绝不丢内容**：
拆不出来就原样返回，让前端退化成显示全文，而不是显示半截。
"""

from __future__ import annotations

import re
from typing import Any

# 段落标题的各种写法。顺序有意义：长匹配在前，避免「操作步骤」被「操作」先吃掉。
# ⚠️ 标题后面允许直接跟内容（`操作步骤 1.日历点击…`）——
#    单行原文切出来就是这样，用 `$` 会整段匹配不上。
#
# ⚠️ `操作` / `验证` 这种两个字的短标题也必须认：
#    185（14_日历_185.txt）用的就是「操作」「验证」，
#    只认"操作步骤"会让整条用例一个步骤都读不出来。
#    但只能**整行**匹配（标题后是行尾或冒号或数字），
#    否则「操作过程无异常」这种正文会被误判成段标题。
_SEC_STEPS = re.compile(r"^\s*(?:操作步骤|测试步骤|执行步骤|步骤|操作)\s*[:：]?\s*(?=\d|$)")
_SEC_EXPECT = re.compile(r"^\s*(?:预期结果|期望结果|预期|期望|验证|检查)\s*[:：]?\s*(?=\d|$)")
_SEC_PRE = re.compile(r"^\s*(?:前提条件|前置条件|前提|预置条件|准备工作)\s*[:：]?\s*(?=\d|\S)")
# 标题行："测试用例 联想日历_176"
_SEC_TITLE = re.compile(r"^\s*(?:测试用例|用例名称|标题)\s*[:：]?\s*(.+)$")

# 编号："1. " / "1、" / "1) " / "(1) " / "① "
# ⚠️ 最后那个 `(\d+)\s*` 不带分隔符的形态是必需的：真实用例里就有
#    `1点击查看新建事件页面…`（15_日历_15.txt）—— 编号和正文之间什么都
#    没有。只认带分隔符的写法，这份文件整段被当成"没有步骤"。
#    但**必须要求编号后紧跟非空白**，否则 `2024 年` 这种正文会被误判成步骤 2024。
_ITEM = re.compile(
    r"^\s*(?:[（(]\s*(\d+)\s*[)）]|([①-⑳])|(\d+)\s*[.、)．]\s*|(\d+)(?=[^\s\d]))\s*(.*)$")

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _num_of(m: re.Match) -> int:
    """从编号匹配里取出序号。拿不到就返回 0（后面靠顺序兜底）。"""
    if m.group(1):
        return int(m.group(1))
    if m.group(2):
        return _CIRCLED.find(m.group(2)) + 1
    if m.group(3):
        return int(m.group(3))
    if m.group(4):
        return int(m.group(4))
    return 0


def _split_inline(text: str) -> list[str]:
    """把"全挤在一行里"的原文按段落标题切开。

    真实存在这种写法（01_日历_168.txt 整份就是一行）：

        测试用例 联想日历_168 前提：… 操作步骤 1.日历点击… 预期结果 1.可打开…

    没有换行可依赖，只能在**段落标题的第一次出现处**断开。

    ⚠️ 两个必须守住的边界（第一版两条都踩了，63 份用例全被切碎）：
      1. 只在**空白之后**才是候选断点 —— 否则「操作步骤」四个字
         出现在句子中间（比如「按上面操作步骤执行」）也会被切断。
      2. 断点前必须有空白，且**标题后面要跟着冒号或数字**，
         这样「测试用例 联想日历_168」这种标题行才不会被再切一次。
    """
    heads = (r"(?:测试用例|用例名称|前提条件|前置条件|预置条件|准备工作"
             r"|操作步骤|测试步骤|执行步骤|预期结果|期望结果|验证|操作)")
    # 复合形态：标题 + (冒号 或 后面紧跟数字编号)。
    #
    # ⚠️ 断点前**不要求空白**：169 的原文是「（清理APP数据 ）操作步骤 1.…」，
    #    中文和标题之间没有空格，要求空白就整段切不开。
    #    靠"标题后面必须跟冒号或数字"这一条来防误切 ——
    #    「按上面操作步骤执行」这种正文后面跟的是汉字，不会被切。
    pat = re.compile(r"(?=" + heads + r"\s*(?:[:：]|\d))")
    parts = [p for p in pat.split(text) if p.strip()]
    return parts or [text]


def _split_items(line: str) -> list[str]:
    """把一行里的多个编号条目切开：`1.甲 2.乙 3.丙` -> ['1.甲', '2.乙', '3.丙']。

    只在"空白 + 编号 + 分隔符/非空白"处切，避免把 `第 3 步` 里的 3
    或者日期、数字当成分段点。
    """
    pat = re.compile(r"(?<=\s)(?=\d{1,2}\s*[.、)．])")
    parts = [p for p in pat.split(line) if p.strip()]
    return parts or [line]


def parse_raw_case(text: str) -> dict[str, Any]:
    """拆原始用例。永远返回完整结构，拆不出来时 steps/expects 为空、raw 照给。

    返回：
        {
          "title": "联想日历_176",
          "precondition": "确保设备没有课程表。可以清空APP数据",
          "steps":  [{"n":1,"text":"..."}, ...],
          "expects":[{"n":1,"text":"..."}, ...],
          "raw": "原文",
          "parsed": True/False      # False = 没认出结构，前端该显示原文
        }
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: dict[str, Any] = {"title": "", "precondition": "", "steps": [],
                           "expects": [], "raw": raw, "parsed": False}
    if not raw.strip():
        return out

    # ★ 先按"段落标题"切一次。
    #   单行挤在一起的原文（168）全靠这一步；正常带换行的文件
    #   切完只是把标题独立成行，行为不变。
    lines: list[str] = []
    for chunk in _split_inline(raw):
        lines.extend(chunk.splitlines())
    lines = [ln for ln in lines if ln.strip()]
    section = ""          # "" / "pre" / "steps" / "expects"
    buckets: dict[str, list[tuple[int, str]]] = {
        "pre": [], "steps": [], "expects": []}

    for ln in lines:
        m_title = _SEC_TITLE.match(ln)
        # 标题只在**还没进任何段落**时才认，避免把正文里的"用例"字样当标题
        if m_title and not section and not out["title"]:
            out["title"] = m_title.group(1).strip()
            continue
        # ★ 段落标题后面可能**紧跟着内容**（单行原文切出来就是这样：
        #   `操作步骤 1.xx 2.yy`）。所以不能 continue ——
        #   要把标题剥掉，剩下的部分照常当步骤/预期处理。
        #   第一版这里直接 continue，结果 168 那种单行原文"什么都没读到"。
        for name, pat in (("pre", _SEC_PRE), ("steps", _SEC_STEPS),
                          ("expects", _SEC_EXPECT)):
            m_sec = pat.match(ln)
            if m_sec:
                section = name
                ln = ln[m_sec.end():]
                break
        if not section:
            continue
        if not ln.strip():
            continue
        # ★ 一行里可能挤了好几步（单行原文切出来就是 `1.xx 2.yy 3.zz`）。
        #   按"空白 + 编号"再切一次，否则会变成"第 1 步"里塞着三步的全部文字。
        for piece in _split_items(ln):
            item = _ITEM.match(piece)
            if item:
                buckets[section].append((_num_of(item), item.group(5).strip()))
            elif buckets[section] and piece.strip():
                # 不是新条目 -> 接上一行（"·课程名称（必填）教室（非必填）"
                # 这种子项必须并进上一条预期，否则会被当成孤立行丢掉）
                n0, t0 = buckets[section][-1]
                buckets[section][-1] = (n0, (t0 + "\n" + piece.strip()).strip())
            elif section == "pre" and piece.strip():
                buckets["pre"].append((0, piece.strip()))

    if buckets["pre"]:
        # 去掉开头残留的冒号/空白 —— `前提：` 这种切完会留下一个孤零零的「：」。
        _pre = "\n".join(t for _n, t in buckets["pre"]).strip()
        out["precondition"] = _pre.lstrip(":：").strip()

    def _build(pairs: list[tuple[int, str]]) -> list[dict[str, Any]]:
        """按出现顺序重编号 —— 原文编号常常写错或漏（比如 3 后面直接 5），
        照抄会让"第 N 步"和"第 N 条预期"对不上。"""
        return [{"n": i + 1, "text": t} for i, (_n, t) in enumerate(pairs)]

    out["steps"] = _build(buckets["steps"])
    out["expects"] = _build(buckets["expects"])
    # 至少要有步骤或预期，才算"读懂了结构"
    out["parsed"] = bool(out["steps"] or out["expects"])
    return out
