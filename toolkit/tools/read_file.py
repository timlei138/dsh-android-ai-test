#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""read_file.py —— 读知识卡 / 路径卡 / 用例（UTF-8，永不乱码）。

## 为什么需要它

知识卡和路径卡都是 UTF-8 无 BOM，而 Windows PowerShell 5.1 的
`Get-Content` 默认按 GBK 解析，必然乱码。实测（2026-10-28 11:35）：

    AI 看到：# ZUI 鏃ュ巻锛堣仈鎯冲钩鏉匡級
    真实内容：# ZUI 日历（联想平板）

乱码 = 那些"坑"等于没读到，直接影响判定准确性。

## 四层递进

    --query  "怎么在课程表里加一节课"   自然语言检索，匹配不到给目录
    --section "课程表空状态"            按章节名读一整节
    --grep    "iv_add_hint"            精确子串，返回命中行 ± 上下文
    （都不给）                          只给章节目录

## 为什么默认只给目录

知识卡整份塞进上下文是浪费。先给章节目录（便宜），
AI 看完自己挑一节再精确读，比乱试 grep 准得多。

## query 怎么打分

"怎么在课程表里加一节课" 用纯 grep 匹配不到（卡里没这句原话）。所以：

  ① 把 query 切成中文 2-gram 和英文词：怎么/么在/在课/课程/程表/...
  ② 同义词扩展：加号 → 添加/新建/plus/+/iv_add_hint
  ③ 每个 token 在「标题」里出现权重 5，在正文里权重 1
  ④ 章节得分 = 该节内所有 token 命中权重之和 / 该节行数（长度归一）
  ⑤ 取前 5 节返回，带摘要（每节前 3 行非空内容）

匹配不到（最高分 0）就返回完整目录，并说明"看 toc 挑一节用 --section"。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# stdin JSON 归一（标准 {"args": [...]} 和扁平字段都认）。
# 放这里做同一份实现，别在 main() 里再抄一遍 —— 抄出来的那份必然只支持
# 一种写法，而 AI 猜不到该用哪种。
from common import payload_to_argv

# ── UTF-8 兜底 ────────────────────────────────────────────────────
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent

# ── 同义词扩展表 ──────────────────────────────────────────────────
#
# 只放**实测中 AI 真会用到的说法**。不做大而全的同义词库 ——
# 那会让不该命中的章节也命中。
SYNONYMS: dict[str, tuple[str, ...]] = {
    "加号": ("添加", "新建", "plus", "iv_add_hint"),
    "添加": ("加号", "新建", "create"),
    "新建": ("添加", "create"),
    "删除": ("移除", "delete", "remove"),
    "清空": ("清除", "delete", "clear", "pm clear"),
    "权限": ("permission", "授权", "同意"),
    "引导": ("guide", "弹框", "首次"),
    "弹窗": ("弹框", "dialog", "popup"),
    "弹框": ("弹窗", "dialog"),
    "滚轮": ("wheel", "picker", "滑动选择"),
    "周数": ("week", "总周数"),
    "课程": ("course", "课表"),
    "课表": ("课程", "timetable"),
    "导入": ("import",),
    "图库": ("gallery", "相册", "photos"),
    "拍照": ("camera", "相机"),
    "置灰": ("disabled", "enabled", "不可点"),
    "崩溃": ("crash", "闪退", "anr"),
    "空态": ("empty", "空状态", "还未添加"),
}

# 中文虚词：切 token 时丢掉，否则每个 query 都能命中所有章节
STOP_TOKENS = frozenset({
    "怎么", "什么", "如何", "可以", "是否", "这个", "那个", "时候",
    "的话", "一个", "就能", "就能", "到底", "为什", "什么",
})


def _tokens(text: str) -> set[str]:
    """把一句话切成可比对的 token：中文 2-gram + 英文/数字词。"""
    t = (text or "").lower()
    out: set[str] = set()
    # 英文和数字：连续 2 字符以上
    for m in re.finditer(r"[a-z0-9_]{2,}", t):
        out.add(m.group(0))
    # 中文：连续汉字段里的 2-gram
    for seg in re.finditer(r"[\u4e00-\u9fff]{2,}", t):
        s = seg.group(0)
        for i in range(len(s) - 1):
            out.add(s[i:i + 2])
    # 同义词扩展
    for key, alts in SYNONYMS.items():
        if key in t:
            out.update(a.lower() for a in alts)
    return {x for x in out if x and x not in STOP_TOKENS}


def _parse_sections(lines: list[str]) -> list[dict]:
    """按 markdown 标题切章节。返回 [{level, title, start, end}]（start/end 为 1-based 行号）。"""
    heads: list[tuple[int, int, str]] = []   # (行号, level, 标题)
    for i, ln in enumerate(lines, 1):
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    out: list[dict] = []
    for idx, (start, level, title) in enumerate(heads):
        # 本节到下一个同级或更高级标题为止
        end = len(lines)
        for nxt_start, nxt_level, _ in heads[idx + 1:]:
            if nxt_level <= level:
                end = nxt_start - 1
                break
        out.append({"level": level, "title": title,
                    "start": start, "end": end})
    return out


def _preview(lines: list[str], start: int, end: int,
             max_lines: int = 3) -> str:
    """取该节前几行有内容的文字，做摘要。"""
    out: list[str] = []
    for ln in lines[start:end]:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        # 去掉列表符号和加粗
        s = re.sub(r"^[-*+]\s+", "", s)
        s = s.replace("**", "")
        out.append(s)
        if len(out) >= max_lines:
            break
    return " / ".join(out)[:160]


# query 模式下每节给多少字全文。
#
# 只给摘要的话，AI 拿到摘要后会**再用 path=+section 把同一节重读一遍**拿全文
# （实测一轮 10 次 read_file 里 5 次读的正是 query 已给过摘要的节）。
# 直接给全文，一次往返就够。
# 设上限是因为个别长节可能上千字，8 节全给就爆了。
MODAL_FULL_CAP = 1200


def _section_body(lines: list[str], start: int, end: int,
                  cap: int = MODAL_FULL_CAP) -> tuple[str, bool]:
    """取该节全文（去掉标题行）。返回 (正文, 是否被截断)。"""
    body = "\n".join(lines[start:end]).strip()
    # 去掉第一行的标题（标题已单独返回，正文里不用重复）
    if body.startswith("#"):
        body = "\n".join(body.splitlines()[1:]).strip()
    if len(body) > cap:
        return body[:cap], True
    return body, False


def _score_sections(lines: list[str], sections: list[dict],
                    tokens: set[str]) -> list[dict]:
    """给每节打分。标题命中权重 5，正文命中权重 1，按长度归一。"""
    scored: list[dict] = []
    for sec in sections:
        title_l = sec["title"].lower()
        body = "\n".join(lines[sec["start"]:sec["end"]]).lower()
        n_lines = max(1, sec["end"] - sec["start"])
        score = 0.0
        hits: list[str] = []
        for tk in tokens:
            if tk in title_l:
                score += 5.0
                hits.append(tk)
            elif tk in body:
                score += 1.0
                hits.append(tk)
        if score <= 0:
            continue
        # 长度归一：长章节不该因为字多而占便宜
        norm = score / (1.0 + n_lines / 30.0)
        scored.append({
            "title": sec["title"],
            "start": sec["start"],
            "end": sec["end"],
            "score": round(norm, 2),
            "hits": sorted(set(hits))[:8],
        })
    scored.sort(key=lambda x: -x["score"])
    return scored


def _toc(lines: list[str], sections: list[dict],
         max_items: int = 200) -> list[dict]:
    """章节目录：标题 + 行号 + 摘要。"""
    out: list[dict] = []
    for sec in sections[:max_items]:
        # 只列 1-3 级，4 级太细
        if sec["level"] > 3:
            continue
        out.append({
            "title": sec["title"],
            "line": sec["start"],
            "level": sec["level"],
            "preview": _preview(lines, sec["start"], sec["end"]),
        })
    return out


def _grep(lines: list[str], needle: str,
          ctx: int = 2) -> list[dict]:
    """精确子串匹配，返回命中行 ± 上下文。"""
    nl = needle.lower()
    out: list[dict] = []
    for i, ln in enumerate(lines, 1):
        if nl and nl in ln.lower():
            a = max(1, i - ctx)
            b = min(len(lines), i + ctx)
            out.append({
                "line": i,
                "text": ln.strip()[:200],
                "context": [x.strip()[:180] for x in lines[a - 1:b]],
            })
            if len(out) >= 20:
                break
    return out


def _resolve(path_arg: str) -> Path:
    p = Path(path_arg)
    if not p.is_absolute():
        p = ROOT / path_arg
    return p


# ══════════════════════════════════════════════════════════════════
# 卡片模式：一次同时搜知识卡 + 路径卡
# ══════════════════════════════════════════════════════════════════
#
# ## 为什么需要
#
# 这两张卡是**两个独立文件**，而且互不指路：
#
#     knowledge/<包名>.md           是什么、为什么、怎么算符合预期
#     knowledge/paths/<包名>.md     在哪找什么（节点与锚点）
#
# 知识卡里提到「路径图」但**没写文件在哪**；只有路径卡反过来提了知识卡。
# 结果：只读知识卡的 AI 会漏掉整个路径图（从哪个页面点什么到哪）。
#
# 实测（2026-10-28）：176 那条用例的 AI 一直在猜 activity 名，
# 而路径卡里就写着「课程表页-空态 / 课程表页-周视图」的节点和锚点。
#
# ## 所以：card= 参数一次搜两张
#
#     --card com.zui.calendar --query "怎么加课程"
#
# 结果按来源标注 [知识卡] / [路径卡]，让 AI 知道哪条来自哪里。


def _card_paths(pkg: str) -> list[tuple[str, Path]]:
    """某应用的卡片文件：[(来源标签, 路径)]。"""
    out: list[tuple[str, Path]] = []
    k = ROOT / "assets" / "knowledge" / f"{pkg}.md"
    if k.is_file():
        out.append(("知识卡", k))
    p = ROOT / "assets" / "knowledge" / "paths" / f"{pkg}.md"
    if p.is_file():
        out.append(("路径卡", p))
    return out


def _search_one(filepath: Path, query: str | None,
                section: str | None, grep: str | None,
                per_file: int = 4) -> dict:
    """在一张卡里搜，返回结构化结果。供 _card_mode 复用。"""
    text = filepath.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    sections = _parse_sections(lines)
    base = {"file": str(filepath), "total_lines": len(lines),
            "sections": len(sections)}

    if grep:
        return {**base, "mode": "grep",
                "matches": _grep(lines, grep)[:8]}

    if section:
        want = section.strip().lower()
        picked = [s for s in sections if want in s["title"].lower()]
        if not picked:
            toks = _tokens(section)
            cand = []
            for s in sections:
                tl = s["title"].lower()
                hit = sum(1 for t in toks if t in tl)
                if hit:
                    cand.append((hit, s))
            cand.sort(key=lambda x: -x[0])
            picked = [s for _, s in cand[:2]]
        if picked:
            sec = picked[0]
            body = "\n".join(lines[sec["start"] - 1:sec["end"]])
            return {**base, "mode": "section", "section": sec["title"],
                    "line": sec["start"], "content": body[:4000]}
        return {**base, "mode": "section", "found": False}

    if query:
        toks = _tokens(query)
        scored = _score_sections(lines, sections, toks)
        if scored:
            top = scored[:per_file]
            hits = []
            for m in top:
                body, cut = _section_body(lines, m["start"] - 1, m["end"])
                item = {"title": m["title"], "score": m["score"],
                        "body": body}
                if cut:
                    item["truncated"] = True
                hits.append(item)
            return {**base, "mode": "query", "matches": hits}
        return {**base, "mode": "query", "matches": []}

    return {**base, "mode": "toc", "toc": _toc(lines, sections, 40)}


def _card_mode(pkg: str, query: str | None, section: str | None,
               grep: str | None) -> dict:
    """同时搜知识卡 + 路径卡，按来源标注。"""
    cards = _card_paths(pkg)
    if not cards:
        return {"ok": False,
                "error": f"找不到 {pkg} 的卡片",
                "tried": [str(ROOT / "assets" / "knowledge" / f"{pkg}.md"),
                          str(ROOT / "assets" / "knowledge" / "paths"
                              / f"{pkg}.md")]}
    results = []
    for label, fp in cards:
        try:
            r = _search_one(fp, query, section, grep)
        except OSError as exc:
            r = {"file": str(fp), "error": str(exc)}
        r["source"] = label
        r["name"] = fp.name
        results.append(r)

    out: dict = {
        "ok": True,
        "mode": "card",
        "package": pkg,
        "query": query, "section": section, "grep": grep,
        "cards": results,
    }
    # 给一句汇总，AI 一眼知道两边各命中多少
    summary = []
    for r in results:
        n = len(r.get("matches") or []) or len(r.get("toc") or [])
        summary.append(f"{r['source']}({r['name']}): {n} 项")
    # ⚠️ 别教 --path。实测教了之后 AI 就一路 path=+section 单节读，
    #    一轮读了 9 次。card= 能一次搜两张卡、section 还能重复传多节。
    if query:
        out["hint"] = ("；".join(summary)
                       + f"。上面每个 match 的 body 已是小节全文，"
                         f"不用再读一遍。需要别的节就用 "
                         f'--card {pkg} --section <节名>（section 可重复传多个）')
    else:
        out["hint"] = ("；".join(summary)
                       + f"。要读某一节，用 --card {pkg} --section <节名>"
                         "（section 可重复传多个，一次读完所有要看的节）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="读文本文件（UTF-8）")
    ap.add_argument("--path", default=None,
                    help="文件路径（相对工具层根目录）。给了 --card 时不用它")
    ap.add_argument("--card", default=None,
                    help="包名，如 com.zui.calendar。一次同时搜"
                         " assets/knowledge/<包名>.md 和"
                         " assets/knowledge/paths/<包名>.md")
    ap.add_argument("--query", default=None)
    ap.add_argument("--section", action="append", default=None,
                    help="章节名（可重复传多次，一次读多节）")
    ap.add_argument("--grep", default=None)
    ap.add_argument("--offset", type=int, default=None)
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--stdin-json", action="store_true", dest="stdin_json",
                    help="从 stdin 读 JSON {\"args\": [\"--path\", ...]}，"
                         "避免命令行引号被 Windows 拆断")
    args = ap.parse_args()

    # ★ stdin JSON 模式：参数不走命令行
    #
    # 为什么：参数里带双引号时（如 section='坑-…的两种"空"'），
    # 命令行的转义在 Windows cmd 上和 POSIX 不一样，一定被拆断。
    # 走 stdin 之后，引号/空格/&/|/%/中文 都不进命令行解析，彻底安全。
    if args.stdin_json:
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False,
                              "error": f"stdin JSON 解析失败: {exc}"},
                             ensure_ascii=False))
            return 2
        argv = payload_to_argv(payload)
        # 把 JSON 里的参数还原成 argparse 认的形式
        sys.argv = ["read_file.py"] + argv
        args = ap.parse_args()

    # ★ 卡片模式优先：一次搜两张卡，按来源标注
    #
    # --section 现在是 action="append"，可能拿到 list。多节时展开成多次搜，
    # 但**输出要拍平**——嵌套数组 AI 不好读，它要的是「内容」本身。
    if args.card:
        # ⚠️ card 模式下 --path / --offset / --limit 不生效。
        #
        # 实测（2026-10-28）：AI 把它当成「按行读某一张卡的某段」传了
        #   --card com.zui.calendar --path paths/xxx.md --offset 546 --limit 20
        # 实际拿到两张卡的目录。**不报错，结果错** —— 最坑的那种。
        #
        # 所以现在显式告知被忽略，并给出正确用法。
        ignored: list[str] = []
        if args.path:
            ignored.append("--path")
        if args.offset is not None:
            ignored.append("--offset")
        if args.limit is not None:
            ignored.append("--limit")

        secs = args.section if isinstance(args.section, list) else (
            [args.section] if args.section else [None])
        if len(secs) == 1:
            out = _card_mode(args.card, args.query, secs[0], args.grep)
        else:
            merged: dict = {"ok": True, "mode": "card",
                            "package": args.card,
                            "query": args.query,
                            "sections_asked": secs,
                            "found": [], "content": ""}
            parts: list[str] = []
            for s in secs:
                one = _card_mode(args.card, args.query, s, args.grep)
                for c in one.get("cards") or []:
                    label = c.get("source") or "?"
                    if c.get("content"):
                        parts.append(f"【{label}】{c.get('section', s)}\n"
                                     + c["content"])
                        merged["found"].append({
                            "source": label, "asked": s,
                            "title": c.get("section"),
                            "chars": len(c["content"])})
                    for m in c.get("matches") or []:
                        merged["found"].append({
                            "source": label, "asked": s,
                            "title": m.get("title"), "score": m.get("score")})
                        parts.append(
                            f"【{label}】{m.get('title')}\n"
                            f"{m.get('body') or m.get('preview') or ''}")
            merged["content"] = "\n\n" + ("\n" + "─" * 56 + "\n").join(parts)
            hits_asked = {f["asked"] for f in merged["found"]}
            missed = [s for s in secs if s and s not in hits_asked]
            hint = (f"按 {len(secs)} 个章节搜了两张卡，"
                    f"命中 {len(merged['found'])} 项，"
                    f"{len(merged['content'])} 字符")
            if missed:
                hint += f"；**没找到**：{', '.join(missed)}"
                merged["missing"] = missed
            merged["hint"] = hint
            out = merged

        if ignored:
            out["ignored_params"] = ignored
            out["warning"] = (
                f"card 模式下 {', '.join(ignored)} 不生效（已忽略）。"
                "card 是「一次同时搜知识卡+路径卡」，"
                "要看某个具体文件请改用 --path 而不给 --card；"
                "要读某一节给 --section")
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if not args.path:
        print(json.dumps({
            "ok": False,
            "error": "需要 --path <文件> 或 --card <包名>",
            "hint": "--card com.zui.calendar 一次搜知识卡+路径卡；"
                    "--path 指定具体文件",
        }, ensure_ascii=False))
        return 2

    fp = _resolve(args.path)

    # ★ 支持列目录 —— 2026-10-28 踩过：
    #   AI 调 `--path assets/cases/com.zui.calendar` 想看有哪些用例，
    #   结果报「文件不存在」，于是它退回 bash `dir` 试了 2 次，
    #   白花 6 秒。列目录是合理需求，直接支持。
    if fp.is_dir():
        entries = sorted(fp.iterdir(),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
        items = []
        for p in entries[:200]:
            items.append({
                "name": p.name,
                "type": "dir" if p.is_dir() else "file",
                "size": 0 if p.is_dir() else p.stat().st_size,
            })
        print(json.dumps({
            "ok": True,
            "mode": "dir",
            "path": str(fp),
            "count": len(items),
            "entries": items,
            "hint": ("这是目录。要看某个文件，用 --path <目录>/<文件名>；"
                     "要按主题搜知识卡，用 --card <包名>"),
        }, ensure_ascii=False))
        return 0

    if not fp.is_file():
        # 文件不存在时，顺手给「这个目录下有什么」，省得再试一次
        near = []
        if fp.parent.is_dir():
            near = sorted(p.name for p in fp.parent.iterdir())[:30]
        print(json.dumps({
            "ok": False,
            "error": f"文件不存在: {fp}",
            "root": str(ROOT),
            "parent_exists": fp.parent.is_dir(),
            "nearby": near,
            "hint": ("要读目录请直接给目录路径（会列出内容）；"
                     "nearby 是同目录下实际存在的文件"),
        }, ensure_ascii=False))
        return 1

    try:
        # ★ utf-8-sig：有 BOM 也能读；没 BOM 正常读
        text = fp.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)},
                         ensure_ascii=False))
        return 1

    lines = text.splitlines()
    total = len(lines)
    sections = _parse_sections(lines)

    base = {"ok": True, "path": str(fp), "total_lines": total,
            "sections": len(sections)}

    # ── 模式 1：精确 grep ────────────────────────────────────────
    if args.grep:
        hits = _grep(lines, args.grep)
        print(json.dumps({
            **base, "mode": "grep", "grep": args.grep,
            "found": len(hits), "matches": hits,
            "hint": ("没命中就换 --query 做语义检索，或直接看 toc"
                     if not hits else "用 --offset/--limit 读命中行附近全文"),
        }, ensure_ascii=False))
        return 0

    # ── 模式 2：按章节读（模糊搜主题）────────────────────────────
    #
    # 语义是「搜主题」，不是「精确取一节」：
    #
    #   --section "课程表空状态"   -> 所有标题含「课程表空状态」的节
    #   --section "课程表"         -> 所有标题含「课程表」的节（一次拿全）
    #   --section "加号"           -> 标题没有，退一步按正文找
    #
    # 为什么不做成精确匹配：实测 --section 全名只命中标题完全对应的那一小节
    # （几行），AI 拿到等于没有 —— 它想要的往往是「这个主题相关的所有小节」。
    # 改成模糊搜之后一次给全相邻上下文。
    if args.section:
        # ★ 支持多节：--section A --section B --section C
        #
        # 2026-10-28 实测：176 那轮为了查 4 个页面，逐个调了 6 次
        # （每次 2-3 秒），光查卡就 45 秒。一次给多节能把 6 次压到 1 次。
        wants = args.section if isinstance(args.section, list) else [args.section]
        all_parts: list[str] = []
        all_items: list[dict] = []
        missing: list[str] = []
        MAX_CHARS = 8000
        used = 0

        for want_raw in wants:
            want = want_raw.strip().lower()
            picked = [s for s in sections if want in s["title"].lower()]
            how = "title-exact"

            if not picked:
                # 退一步 1：按 token 匹配标题（按命中数排序，只取前 2 节）。
                #
                # ⚠️ 必须卡上限：`--section "加号"` 切 2-gram 后能匹配 4 个
                # 标题含「新建」「添加」的章节，合并 16014 字符 —— 等于把半个
                # 卡塞进上下文，比不读还糟。
                toks = _tokens(want_raw)
                cand: list[tuple[int, dict]] = []
                for s in sections:
                    tl = s["title"].lower()
                    hit = sum(1 for t in toks if t in tl)
                    if hit:
                        cand.append((hit, s))
                cand.sort(key=lambda x: -x[0])
                picked = [s for _, s in cand[:2]]
                how = "title-token"

            if not picked:
                picked = [s for s in sections
                          if want in "\n".join(
                              lines[s["start"] - 1:s["end"]]).lower()]
                how = "body"

            if not picked:
                missing.append(want_raw)
                continue

            for sec in picked:
                body = "\n".join(lines[sec["start"] - 1:sec["end"]])
                if used + len(body) > MAX_CHARS and all_parts:
                    continue
                all_parts.append(body)
                used += len(body)
                all_items.append({"title": sec["title"],
                                  "line": sec["start"],
                                  "end_line": sec["end"],
                                  "chars": len(body),
                                  "asked": want_raw,
                                  "match_how": how})

        if not all_parts:
            print(json.dumps({
                **base, "mode": "section", "sections_asked": wants,
                "found": False,
                "toc": _toc(lines, sections),
                "hint": ("没有含这些名字的章节。上面是完整目录，"
                         "挑一个用 --section 精确读，或 --query 做语义检索"),
            }, ensure_ascii=False))
            return 0

        content = "\n\n" + ("\n" + "─" * 60 + "\n").join(all_parts)
        hint = f"读到 {len(all_items)} 节（{len(content)} 字符）"
        if missing:
            hint += f"；没找到的：{', '.join(missing)}"
        print(json.dumps({
            **base, "mode": "section",
            "sections_asked": wants,
            "found": len(all_items),
            "sections": all_items,
            "content": content,
            "hint": hint,
        }, ensure_ascii=False))
        return 0

    # ── 模式 3：自然语言检索 ────────────────────────────────────
    if args.query:
        toks = _tokens(args.query)
        scored = _score_sections(lines, sections, toks)
        if not scored:
            print(json.dumps({
                **base, "mode": "query", "query": args.query,
                "tokens": sorted(toks)[:20],
                "matched": 0,
                "toc": _toc(lines, sections),
                "hint": ("没有章节和这个查询相关。上面是完整目录，"
                         "挑一节用 --section 读；或用 --grep 搜具体 rid/文案"),
            }, ensure_ascii=False))
            return 0
        top = scored[:5]
        print(json.dumps({
            **base, "mode": "query", "query": args.query,
            "tokens": sorted(toks)[:20],
            "matched": len(scored),
            "matches": [
                {**m, "preview": _preview(lines, m["start"] - 1, m["end"])}
                for m in top
            ],
            "hint": "挑一节用 --section \"标题\" 读全文",
        }, ensure_ascii=False))
        return 0

    # ── 模式 4：按行读 ──────────────────────────────────────────
    if args.offset is not None:
        a = max(1, args.offset)
        b = min(total, a + max(1, args.limit) - 1)
        print(json.dumps({
            **base, "mode": "lines",
            "offset": a, "end_line": b,
            "content": "\n".join(lines[a - 1:b]),
        }, ensure_ascii=False))
        return 0

    # ── 模式 5：只给目录（默认）─────────────────────────────────
    print(json.dumps({
        **base, "mode": "toc",
        "toc": _toc(lines, sections),
        "first_lines": "\n".join(lines[:12]),
        "hint": ("这是章节目录。用 --section \"标题\" 读某一节，"
                 "--query \"自然语言问题\" 做检索，--grep 搜具体 rid/文案"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
