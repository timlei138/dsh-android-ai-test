"""知识资产体检：查知识卡 / 路径图 / 用例 YAML 三者的一致性。

存在的理由
----------
三类文件各司其职、**有意保留少量重复**（YAML 要自包含，回放时不该再翻卡）。
重复本身没问题，**重复处悄悄变得不一致**才是问题 —— 与"YAML 能解析但中文
已被编码毁掉"、"case 改了但 YAML 没跟上" 属同一类：**看起来正常，实际对不上**。
本脚本就是用来发现这类"静默不一致"的。

检查项
------
  1. 路径图断链   ：「→ 去向」写了一个没有对应 `## 节点` 的名字
  2. 索引不同步   ：「节点索引」表与文中实际的 `## 节点` 对不上
  3. 废弃仍引用    知识卡标注"已移除/改版"的 rid，路径图仍当可用锚点
  4. 引用锚过期    用例 YAML 里 `from: 卡.md#小节` 指向的知识卡小节，
                   内容已变（quote_hash 对不上）→ 该 YAML 的副本可能已过期

关于第 4 项（为什么允许重复、但要能查出过期）
---------------------------------------------
YAML 要**自包含**：回放时不该要求执行者再翻知识卡（翻卡费时且易漏），
所以 `know`/`pitfalls` 里**有意**保留知识卡结论的精简副本。
代价是副本会过期。`from:` + `quote_hash:` 让过期**可被检测** ——
与"YAML 合法但中文被编码毁掉"、"case 改了但 YAML 没跟上"同属一类：
**看起来正常，实际对不上**，必须能查出来。

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


# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ⚠️/✅ 就崩。
#   必须在任何输出之前调用（含 argparse 的报错输出）。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "assets" / "knowledge"
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
    """检查用例 YAML 里的引用锚是否过期。"""
    import yaml

    cases_dir = ROOT / "assets" / "cases"
    if not cases_dir.is_dir():
        return 0
    issues = 0
    cache: dict[str, str] = {}

    for yf in sorted(cases_dir.rglob("*.yaml")):
        try:
            doc = yaml.safe_load(yf.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            print(f"  ⚠️ {yf.name}: 无法解析 —— {e}")
            issues += 1
            continue
        if not isinstance(doc, dict):
            continue
        anchors = collect_anchors(doc)
        title = (doc.get("meta") or {}).get("title") or yf.stem
        if not anchors:
            print(f"  ○ {title}: 无引用锚（可以加，用于发现副本过期）")
            continue
        bad = 0
        for from_ref, want in anchors:
            fname, _, sec = from_ref.partition("#")
            kf = KNOWLEDGE / fname
            if not kf.is_file():
                print(f"  ⚠️ {title}: 引用锚指向不存在的知识卡 {fname}")
                bad += 1
                continue
            if fname not in cache:
                cache[fname] = kf.read_text(encoding="utf-8")
            body = section_text(cache[fname], sec)
            if body is None:
                print(f"  ⚠️ {title}: 引用锚的小节不存在 → {from_ref}")
                bad += 1
            elif want and quote_hash(body) != want:
                print(f"  ⚠️ {title}: 引用已过期 → {from_ref}")
                print(f"     （知识卡该节已改动，YAML 里的副本需复核）")
                bad += 1
        if not bad:
            print(f"  ✅ {title}: {len(anchors)} 个引用锚均有效")
        issues += bad
    return issues


def _normalize_expect(s: str) -> str:
    """归一化用例预期文本，用于"YAML 是否照抄原文"的比对。

    只抹平排版差异（空白、全/半角引号、中英标点），**不动文字内容** ——
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
    """核对每个 YAML 的 `case_expect` 是否**照抄用例原文的预期**。

    存在意义（会话473 复盘）：**改写 case_expect 是静默失真** ——
    182 曾把用例的 6 条预期压缩成 3 条泛化描述、再凭空补 2 条，
    跑完看着"全 PASS"，实则**用例真正要测的东西被抹掉了**，且没有任何工具报错。
    这里只做**提示**（不阻断），因为它依赖会话库里的用例原文：
    原文缺失/拆分口径不同都可能误报，交给人判断。
    """
    import sqlite3
    import yaml

    cases_dir = ROOT / "assets" / "cases"
    from runtime import STORAGE_DIR
    db_path = STORAGE_DIR / "test.db"
    if not cases_dir.is_dir() or not db_path.is_file():
        return 0

    # 收集所有会话里出现过的用例原文，按用例号归并
    rows: list[tuple] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = list(con.execute(
            "SELECT id, user_input FROM sessions"
            " WHERE user_input IS NOT NULL"))
        con.close()
    except sqlite3.Error:
        return 0

    by_num: dict[str, list[tuple[int, str]]] = {}
    for sid, t in rows:
        m = re.search(r"测试用例\s*(\S+)", t or "")
        if m:
            by_num.setdefault(m.group(1).strip(), []).append((sid, t))

    issues = 0
    for yf in sorted(cases_dir.rglob("*.yaml")):
        try:
            doc = yaml.safe_load(yf.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        asserts = doc.get("assertions") or []
        if not asserts:
            continue

        m = re.search(r"_(\d+)_", yf.name) or re.search(r"_(\d+)\.yaml", yf.name)
        num = m.group(1) if m else None
        if not num:
            continue
        src = None
        for key, lst in by_num.items():
            if num not in key:
                continue
            for _sid, t in lst:
                got = _split_expectations(t)
                if got:
                    src = got
                    break
            if src:
                break
        if not src:
            print(f"  ○ {yf.stem}: 无原文可比（会话库缺该编号的用例文本）")
            continue

        title = (doc.get("meta") or {}).get("title") or yf.stem
        src_norm = {_normalize_expect(s) for s in src}
        unmatched = []
        for a in asserts:
            ce = _normalize_expect(str(a.get("case_expect") or ""))
            if ce not in src_norm:
                unmatched.append((a.get("id"), a.get("case_expect")))
        if unmatched:
            print(f"  ⚠️ {title}: {len(unmatched)} 条 case_expect 与用例原文不符")
            for aid, ce in unmatched:
                print(f"     [{aid}] {str(ce)[:64]}")
            print("     （case_expect 必须照抄原文预期；改写/合并会让「哪条预期挂了」失真）")
            issues += len(unmatched)
        elif len(asserts) != len(src):
            # 文本都对得上，只是条数不同 —— 通常是"一条预期拆成多个判据"，属允许
            print(f"  ○ {title}: {len(asserts)} 条断言 vs 原文 {len(src)} 条预期"
                  f"（文本可对应，拆分记录，允许）")
        else:
            print(f"  ✅ {title}: {len(asserts)} 条 case_expect 均照抄原文")
    return issues


def collect_anchors(node, out=None) -> list[tuple[str, str]]:
    """从 YAML 里递归收集 {from: '...', quote_hash: '...'} 形式的引用锚。"""
    if out is None:
        out = []
    if isinstance(node, dict):
        f = node.get("from")
        if isinstance(f, str) and "#" in f:
            out.append((f, str(node.get("quote_hash") or "")))
        for v in node.values():
            collect_anchors(v, out)
    elif isinstance(node, list):
        for v in node:
            collect_anchors(v, out)
    return out


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

    print("\n── 用例 YAML 引用锚 ──")
    total += check_case_specs()

    print("\n── 用例 YAML 的 case_expect 是否照抄原文 ──")
    total += check_case_expectations()

    print()
    if total:
        print(f"共 {total} 项待办 —— 这些是『看起来正常但实际对不上』的地方，请逐条确认。")
        return 1
    print("✅ 知识资产一致，无待办项。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
