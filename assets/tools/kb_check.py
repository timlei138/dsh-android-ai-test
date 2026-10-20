"""知识资产体检：查知识卡 / 路径图 的一致性。

存在的理由
----------
两类文件各司其职、**有意保留少量重复**。
重复本身没问题，**重复处悄悄变得不一致**才是问题 —— 与"YAML 能解析但中文
已被编码毁掉"属同一类：**看起来正常，实际对不上**。
本脚本就是用来发现这类"静默不一致"的。

检查项
------
  1. 路径图断链   ：「→ 去向」写了一个没有对应 `## 节点` 的名字
  2. 索引不同步   ：「节点索引」表与文中实际的 `## 节点` 对不上
  3. 废弃仍引用    知识卡标注"已移除/改版"的 rid，路径图仍当可用锚点

关于「用例 YAML 的引用锚」为什么去掉了
--------------------------------------
旧版还查第 4 项：YAML 里 `from: 卡.md#小节` + `quote_hash` 指向的知识卡小节
是否变了。现在**整块移除**，因为：

  · `StepSpec.pitfalls` 是 `list[str]` —— 引用锚（dict 形式）会变成
    **模型读不到的字典**，坑等于白写。`case_spec_check.py` 已把它判成 DEAD。
  · 引用锚本质是**知识卡的维护职责**，不该塞进用例的步骤坑里。
    步骤坑只回答一个问题：*走到这步，我该知道什么？*
  · 用例 YAML 由 `case_spec_check.py` 管格式；`case_expect` 也不再是 YAML 字段
    （`AssertSpec` 只有 id/desc/check），所以"是否照抄原文"改为**人审**。

用法
----
    python tools/kb_check.py                 # 检查全部
    python tools/kb_check.py --package com.zui.calendar
退出码：0 = 全通过；1 = 有待办项（见输出）。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge"
PATHS = KNOWLEDGE / "paths"

# 「去向」允许是这些**页内动作/弹层**，不必单独立节点（有意为之）
INLINE_LAYERS = (
    "弹层", "弹框", "确认框", "提示框", "同节点", "回上一步", "多选态",
    "回到今天", "直接创建", "Activity`",
)


def section_text(md: str, section: str) -> str | None:
    """取 `## section`（或 `### section`）到下一个同级/更高级标题之间的正文。

    匹配规则（放宽，避免因标题写法差异产生假报警）：
      · 忽略空白与破折号差异；
      · 允许小节名带后缀说明 —— 如引用写 `链路-导入弹窗顺序`，
        实际标题是 `链路-导入弹窗顺序（拍照/图库通用，先读这条）`，应能命中。
    """
    want = re.sub(r"[\s\-—－]+", "", section)
    lines = md.split("\n")
    start = None
    level = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#{2,4})\s+(.+?)\s*$", line)
        if not m:
            continue
        title = re.sub(r"[\s\-—－]+", "", m.group(2))
        base = re.sub(r"[（(].*$", "", title)      # 去掉括号后缀
        if start is None:
            if title == want or base == want or title.startswith(want):
                start = i + 1
                level = len(m.group(1))
        elif len(m.group(1)) <= level:
            return "\n".join(lines[start:i]).strip()
    if start is not None:
        return "\n".join(lines[start:]).strip()
    return None


def quote_hash(text: str) -> str:
    """小节内容的指纹：归一化空白后取 sha1 前 8 位。

    归一化空白是为了让"重新排版"不算内容变化 —— 只有**实质改动**才报警。
    """
    import hashlib
    norm = re.sub(r"\s+", "", text or "")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


def check_case_specs() -> int:
    """已废弃 —— 用例 YAML 的格式与引用锚检查交给 case_spec_check.py。

    保留函数壳是为了让 `main()` 的调用点不必改动（且旧调用方不会 AttributeError），
    但它**什么都不做**，永远返回 0。

    为什么废弃：
      · 引用锚（`from` + `quote_hash`）是 dict，而 `StepSpec.pitfalls` 是
        `list[str]` —— 模型读不到这种坑，`case_spec_check.py` 已判 DEAD。
      · 引用锚属于知识卡的维护职责，不该塞进用例的步骤坑。
    """
    return 0


def _normalize_expect(s: str) -> str:
    """【已废弃保留】旧的 case_expect 归一化逻辑。

    现在 `AssertSpec` 只有 id/desc/check，**没有 `case_expect` 字段**，
    所以这块比对无处可依。留在此处仅供将来若要恢复"断言 vs 原文"人审辅助时参考。

    归一化：只抹平排版差异（空白、全/半角引号、中英标点），**不动文字内容** ——
    这样"改写/合并/丢句子"会被抓出来，而"全角引号写成半角"不会误报。
    """
    s = s.replace("\u3000", "")
    s = re.sub(r"\s+", "", s)
    for a, b in (("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"), ("\u2019", "'"),
                 ("\uff1b", ";"), ("\uff0c", ","), ("\uff1a", ":"), ("\u3002", ".")):
        s = s.replace(a, b)
    return s


def _split_expectations(text: str) -> list[str] | None:
    """把用例原文的「预期结果」段切成**逻辑预期**列表。

    两条切割规则（都要，实测两种排版都出现过）：
    ① **换行**：一行以 `N.`/`N．`/`N、` 开头 = 新预期；否则视为上一行的续行（合并）。
       ⚠️ 必须做续行合并：182 的预期4 在原文里横跨 6 行，逐行切会把它拆成 6 条。
    ② **同一行内**：`…3. 验证可进入图片选择入口 预期结果 1. 可打开课程表页面 2. 图库导入
       入口可点击 3. …`（会话178）—— 整段挤在一行、用空格分隔编号。
       故切完换行后，再按"行内出现的 `N.` 编号"二次切分（编号须递增，避免把
       "1~12到1~12节" 这类内容误当编号）。
    """
    m = re.search(r"预期结果[：:]?", text or "")
    if not m:
        return None
    out: list[str] = []
    for ln in text[m.end():].splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if re.match(r"^\d+\s*[.．、]", ln):
            out.append(ln)
        elif out:
            out[-1] += ln
    if not out:
        return None

    # ② 行内二次切分：找 `空格 + 递增编号 + 分隔符` 的位置
    #    ⚠️ 编号后**可能有空格**（168 是 `1. 可打开`，169 是 `1.弹出`）→ 空格可选。
    split: list[str] = []
    for chunk in out:
        parts = re.split(r"\s+(?=\d{1,2}\s*[.．、]\s*\S)", chunk)
        split.extend(p.strip() for p in parts if p.strip())
    return split or out


def check_case_expectations() -> int:
    """已废弃 —— `case_expect` 不再是 YAML 字段，永远返回 0。

    原先做的是「核对 YAML 的 `case_expect` 是否照抄用例原文的预期」，
    存在意义是防**静默失真**（182 曾把 6 条预期压缩成 3 条泛化描述）。
    但 `AssertSpec` 只有 `id` / `desc` / `check` 三个字段，没有 `case_expect`：

      · 写进 YAML 会被 `case_spec_check.py` 判成 DEAD 字段
      · 所以"断言是否忠于原文"这件事，**YAML 层面已无从比对**

    ⇒ 改为**人审**：`assertions[].desc` 必须能对应回用例原文的一条预期。
      `case_spec_check.py` 会强制 `assertions` 非空、每条有 id/desc/check。
    """
    return 0


def check_package(pkg: str) -> int:
    pf = PATHS / f"{pkg}.md"
    kf = KNOWLEDGE / f"{pkg}.md"
    if not pf.is_file():
        print(f"[跳过] {pkg}: 无路径图")
        return 0
    p = pf.read_text(encoding="utf-8")
    k = kf.read_text(encoding="utf-8") if kf.is_file() else ""
    issues = 0

    nodes = {m.strip() for m in re.findall(r"^## (.+)$", p, re.M)}
    real_nodes = {n for n in nodes
                  if not n.startswith("⚠️") and "索引" not in n and "自动采集" not in n}
    # 索引里登记为 ⚠️ 的 = "知道有这个页面、还没单独建节点"，出边指向它们不算断链
    absent_marked = {m.strip() for m in re.findall(r"^\|\s*⚠️\s*(.+?)\s*\|", p, re.M)}

    print(f"── {pkg} ──  节点 {len(real_nodes)} 个")

    # ① 断链：只看出边表
    broken = []
    in_edges = False
    for i, line in enumerate(p.split("\n"), 1):
        s = line.strip()
        if s.startswith("**出边**"):
            in_edges = True
            continue
        if s.startswith("## ") or s.startswith("---"):
            in_edges = False
            continue
        if not in_edges or not s.startswith("|") or set(s) <= set("|-: "):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 5 or cells[-1].endswith("去向"):
            continue
        dest = cells[-1].split("→")[-1].strip().strip("*").strip()
        dest = re.sub(r"（.*?）", "", dest).strip()
        dest = re.split(r"[；，]", dest)[0].strip()
        if not dest or dest == "—":
            continue
        if any(w in dest for w in INLINE_LAYERS):
            continue
        if dest in real_nodes or any(
                n.startswith(dest) or dest.startswith(n) for n in real_nodes):
            continue
        # 已在索引里用 ⚠️ 登记为"知道有、还没建节点"的，不算断链
        if any(dest and (dest in a or a in dest) for a in absent_marked):
            continue
        broken.append((i, dest))
    if broken:
        issues += len(broken)
        print(f"  ⚠️ 断链 {len(broken)} 处（去向无对应节点）")
        for i, d in broken:
            print(f"     L{i}: 「{d}」")
    else:
        print("  ✅ 出边无断链")

    # ② 索引同步：只解析「## 节点索引」到下一个 `---` / `##` 之间的那张表
    idx = set()
    in_idx = False
    for line in p.split("\n"):
        s = line.strip()
        if s.startswith("## 节点索引"):
            in_idx = True
            continue
        if in_idx and (s.startswith("## ") or s.startswith("---")):
            break
        if not in_idx or not s.startswith("|") or set(s) <= set("|-: "):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and "认出它" not in cells[0]:
            idx.add(re.sub(r"^⚠️\s*", "", cells[0]).strip())
    missing = {n for n in real_nodes
               if n not in idx and not any(n in i or i in n for i in idx)}
    if missing:
        issues += len(missing)
        print(f"  ⚠️ 节点索引漏列 {len(missing)} 个: {sorted(missing)}")
    else:
        print("  ✅ 索引与节点一致")

    # ③ 废弃 rid 仍当锚点
    DEP = ("已移除", "已不成立", "旧链路", "已废弃")
    dep_bad = 0
    for i, line in enumerate(k.split("\n"), 1):
        if not any(d in line for d in DEP):
            continue
        for r in re.findall(r"`([a-z_][a-z0-9_]{4,})`", line):
            for j, pl in enumerate(p.split("\n"), 1):
                s = pl.strip()
                if s.startswith("|") and "→" in s and f"`{r}`" in s:
                    dep_bad += 1
                    print(f"  ⚠️ {r}: 卡 L{i} 称已废弃，图 L{j} 仍作锚点")
    if not dep_bad:
        print("  ✅ 无废弃 rid 被当锚点")
    issues += dep_bad

    return issues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package")
    args = ap.parse_args()

    if args.package:
        pkgs = [args.package]
    else:
        pkgs = sorted(f.stem for f in PATHS.glob("*.md"))
    if not pkgs:
        print("没有可检查的路径图")
        return 0

    total = sum(check_package(p) for p in pkgs)

    # ⚠️ 用例 YAML 的检查**不在这里**。
    # 旧版这里有「引用锚过期」和「case_expect 是否照抄原文」两项，现已移除：
    #   · 引用锚是 dict，而 StepSpec.pitfalls 是 list[str]，模型读不到 → 已废弃
    #   · case_expect 不再是 YAML 字段（AssertSpec 只有 id/desc/check）→ 无从比对
    # 用例 YAML 的格式与字段合法性由 `case_spec_check.py` 全权负责。
    print("\n（用例 YAML 的格式检查请用 tools/case_spec_check.py）")

    print()
    if total:
        print(f"共 {total} 项待办 —— 这些是『看起来正常但实际对不上』的地方，请逐条确认。")
        return 1
    print("✅ 知识资产一致，无待办项。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
