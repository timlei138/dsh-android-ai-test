"""知识卡：目录化 + 按需查询。

## 为什么需要这个模块

知识卡是子 Agent 的核心信息来源 —— 里面写着"这个 App 的坑在哪、
哪个 rid 找不到、滚轮怎么点"。但一份日历知识卡有 34552 字符，
整份塞进上下文不现实（≈1.7 万 token/轮 × 几十轮）。

早先的做法是"首轮注入前 4000 字符"--但那是**截断**：
子 Agent 拿到的是残缺的知识卡，而且它不知道后面还有 88% 的内容。
实测后果：跑 168 时模型引用的全是知识卡开头那几条，
轮次统计里 `knowledge: 0` —— 它从没意识到"可以去查更多"。

现在的做法：
  1. 首轮注入**目录**（有哪些章节、各占多少行）—— 便宜，且告诉模型"有这些"
  2. 模型需要细节时，按章节标题或关键词**按需取**

## 章节怎么切

按 Markdown 标题（`## ` / `### `）切。这不是随便选的：
知识卡的作者就是按"一个坑 / 一个页面"分节的，标题本身就是最好的索引。
切出来的单元大小也合适（几十行到一两百行），一次取一节不会爆上下文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 标题行：# 一级（整个 App）、## 二级（一类坑/页面）、### 三级（具体条目）
#
# 只认到三级：四级以下在知识卡里几乎没用过，多认一级只会让目录变长。
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")

# 一次查询最多返回多少行。超了就截断并提示"用更具体的关键词"，
# 而不是默默给一半 —— 默默截断是这一整个模块想避免的毛病。
MAX_LINES = 120


@dataclass(frozen=True)
class Section:
    """知识卡里的一节。"""
    level: int          # 标题级别 1/2/3
    title: str          # 标题文本（不含 #）
    start: int          # 起始行号（1-based，含标题行）
    end: int            # 结束行号（1-based，含）

    @property
    def n_lines(self) -> int:
        return self.end - self.start + 1


def parse_sections(text: str) -> list[Section]:
    """把一份 Markdown 切成章节列表。

    每节的范围是「自己的标题行」到「下一个同级或更高级标题之前」。
    这样 `## 导航入口` 会包含它下面的所有 `### 子条目`；
    取"导航入口"整节是有意义的（子条目就是它的细节）。
    """
    lines = text.splitlines()
    heads: list[tuple[int, str, int]] = []      # (level, title, 行号 0-based)
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m:
            heads.append((len(m.group(1)), m.group(2).strip(), i))

    out: list[Section] = []
    for idx, (level, title, start) in enumerate(heads):
        # 找下一个 level <= 当前 level 的标题，它就是本节边界
        end = len(lines) - 1
        for nlevel, _, nstart in heads[idx + 1:]:
            if nlevel <= level:
                end = nstart - 1
                break
        out.append(Section(level, title, start + 1, end + 1))
    return out


def outline(skill_root: str, files: list[str]) -> str:
    """生成知识卡目录 —— 首轮注入给子 Agent 的东西。

    比整份文档便宜得多（一份 934 行的卡，目录约 40 行），
    但足够让模型知道"有什么可查"。
    """
    root = Path(skill_root)
    blocks: list[str] = []

    for rel in files:
        path = root / rel
        if not path.exists():
            blocks.append(f"[{rel}]  ⚠️ 文件不存在")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            blocks.append(f"[{rel}]  ⚠️ 读不了：{exc}")
            continue

        secs = parse_sections(text)
        if not secs:
            # 没有标题的文档：给个头尾，让模型知道它存在但没有结构
            n = len(text.splitlines())
            blocks.append(f"[{rel}]  {n} 行，无章节结构")
            continue

        head = [f"[{rel}]  {len(text.splitlines())} 行 / {len(secs)} 节"]
        for s in secs:
            # 只列到三级；一级（App 名）单独当大标题
            indent = "  " * (s.level - 1)
            head.append(f"{indent}- {s.title}  (L{s.start}-{s.end})")
        blocks.append("\n".join(head))

    return "\n\n".join(blocks)


def _match(section_title: str, query: str) -> bool:
    """章节标题是否匹配查询词。

    用「包含」而不是「相等」：模型记不住完整标题，
    它更可能问"滚轮"而不是"⭐ 滚轮要点准，先量「可点带」"。
    """
    q = query.strip().lower()
    return bool(q) and q in section_title.lower()


def read_section(skill_root: str, rel: str, title: str,
                 max_lines: int = MAX_LINES) -> str:
    """取某一节的正文（含标题行）。

    找不到就返回可用章节列表 —— 让模型自己纠错，
    而不是返回一句"没找到"然后它只能干猜。
    """
    path = Path(skill_root) / rel
    if not path.exists():
        return f"[{rel}] 文件不存在"

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    secs = parse_sections(text)

    hit = [s for s in secs if _match(s.title, title)]
    if not hit:
        names = "、".join(s.title for s in secs[:12])
        more = f" 等 {len(secs)} 节" if len(secs) > 12 else ""
        return f"[{rel}] 没有标题含「{title}」的章节。\n可用章节：{names}{more}"

    # 多次命中取第一个 —— 通常是层级最高的那个（父节包含子节）
    sec = hit[0]
    body = lines[sec.start - 1:sec.end]

    if len(body) > max_lines:
        kept = body[:max_lines]
        return ("\n".join(kept)
                + f"\n…（本节还有 {len(body) - max_lines} 行未显示，"
                  f"请用更具体的关键词再查）")

    return "\n".join(body)


def best_section(skill_root: str, files: list[str], query: str,
                 max_lines: int = MAX_LINES) -> str:
    """先挑一个**标题**匹配的章节并返回整节；都不匹配返回空串。

    和 read_section 的区别：这里不报错、不列清单，只是"没挑中"，
    调用方好接着退回关键词检索。分开是因为调用方需要区分
    "章节没中"（换策略）和"用户查的东西真不存在"（告诉他）。

    挑中的规则：优先标题**包含**查询词的长度最短的那节 ——
    短的那节通常更专指（"坑-图库导入解析"优于整个大类"页面-备份与恢复"）。
    """
    imports_root = Path(skill_root)
    cands: list[tuple[int, str, str]] = []   # (标题长度, rel, 标题)

    for rel in files:
        path = imports_root / rel
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for sec in parse_sections(text):
            if _match(sec.title, query):
                cands.append((len(sec.title), rel, sec.title))

    if not cands:
        return ""

    _, rel, title = min(cands)
    return read_section(skill_root, rel, title, max_lines=max_lines)


def search(skill_root: str, files: list[str], keyword: str,
           limit: int = 40) -> str:
    """按关键词全文检索，返回命中行（带出处）。

    和 read_section 的分工：
      · read_section —— "我知道是哪一节，给我整节"
      · search       —— "我不知道在哪，帮我找"
    """
    q = keyword.strip().lower()
    if not q:
        return "（关键词为空）"

    root = Path(skill_root)
    hits: list[str] = []
    for rel in files:
        path = root / rel
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if q in line.lower():
                lo, hi = max(0, i - 1), min(len(lines), i + 2)
                snippet = " / ".join(l.strip() for l in lines[lo:hi] if l.strip())
                hits.append(f"[{rel}:{i + 1}] {snippet[:200]}")
                if len(hits) >= limit:
                    return "\n".join(hits) + f"\n…（已达 {limit} 条上限，请缩小关键词）"

    return "\n".join(hits) if hits else f"（「{keyword}」在这些文档里没有命中）"
