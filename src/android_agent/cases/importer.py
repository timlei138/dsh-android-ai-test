"""把散在文件夹里的原始用例（用户手写的 txt）导入数据库，并关联 YAML。

## 为什么要有这一步

用户的原话是"原始记录在 D:\\dsh\\cal_cases 和 cases 两个文件夹下"。
这两个目录是**用户自己的资料**，不是本工具的产物 —— 工具只能读一次、
导入，之后原文归数据库管。否则网页里改了原文、文件夹里还是老版本，
下一次扫描又把它盖回去（或者反过来），永远说不清哪份是真的。

## 关联是怎么做的

原始用例文件名：`13_日历_167.txt`   （前缀_日历_编号）
YAML 文件名：    `联想日历_167_首次进入课程表引导与空态.yaml`

两边**唯一共有的是编号**。所以：
    cases.case_key = "167"
    cases.yaml_name = "联想日历_167_首次进入课程表引导与空态.yaml"

★ 编号取**最后**一个数字段，不是第一个 —— `13_日历_167.txt` 里
  第一个数字是排序前缀。取错会把 13 号和 167 号认成同一条，且不报错。

## 为什么要"关联"（用户原话：这样删除的时候简单点）

原文和 YAML 是同一条用例的两面。绑在一行里，删用例就是
"删这一行 + 删那个文件"，不会出现"删了原文忘了 YAML"或反过来。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..reporting import store

#: `13_日历_167.txt` —— 前缀_日历_编号
_FILE_RE = re.compile(r"^(\d+)_日历_(\d+)$")

#: `测试用例 联想日历_168 ...` / `测试用例 日历_03 启动应用-CTA权限弹窗（P2）`
_HEAD_RE = re.compile(r"^测试用例\s+(?:联想日历|日历)_(\d+)\s*(.*)$")

#: 优先级：（P2） / (P3) / P1
_PRIO_RE = re.compile(r"[（(]\s*(P\d)\s*[）)]")

#: 首行剩下的东西如果是这些开头，说明原文没有题目，是直接开始写前提的
_NOT_TITLE = ("前提", "预置条件", "操作步骤", "测试步骤", "预期结果")

#: 步骤区的节标题
_STEP_HEAD_RE = re.compile(r"^\s*(操作步骤|测试步骤)\s*[：:]?\s*$", re.M)


def _first_step_title(text: str, limit: int = 20) -> str:
    """从"操作步骤"下的第一条里抠一个说明性短名。

    为什么这么做：`cases` 那批里有 8 条首行只有 `测试用例 联想日历_170`，
    原文确实没有题目。列表里显示一列空标题，用户得逐条点开才知道是啥。
    而第一条步骤本身就是在说"这条用例干什么"——
    `进入已保存的课程表，点击右上角添加按钮`。

    ⚠️ 这是**原文里的字**，不是模型编的标题。抠不出来就返回空，
       宁可显示"未命名"也不要造一个看起来像官方题目的名字。
    """
    body = text or ""
    m = _STEP_HEAD_RE.search(body)
    seg = body[m.end():] if m else body
    for ln in seg.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("预期结果", "前提", "预置条件")):
            break
        s = re.sub(r"^\d+\s*[.、)]\s*", "", s).strip()
        if not s:
            continue
        return s[:limit] + ("…" if len(s) > limit else "")
    return ""


def parse_raw(text: str, *, fallback_title: str = "") -> dict[str, str]:
    """从原文里抠出 标题 / 优先级。抠不出来就留空，绝不瞎猜。

    两批文件格式不一样：
      cal_cases：`测试用例 日历_03 启动应用-CTA权限弹窗（P2）` —— 有题目
      cases：    `测试用例 联想日历_168 前提：确保设备没有课程表…` —— 没题目
    所以题目是否为空，得看首行剩下的那部分**是不是正文**。
    """
    out = {"title": "", "priority": ""}
    body = (text or "").strip()
    if not body:
        return out
    first = body.splitlines()[0].strip()
    m = _HEAD_RE.match(first)
    rest = (m.group(2) if m else first).strip()

    # 整条用例只有一行（cases 那批）时，rest 会是一大段正文 ——
    # 那就不是题目，别把"前提：确保设备没有课程表…"当成标题显示。
    if rest and not rest.startswith(_NOT_TITLE) and len(rest) <= 40:
        prio = _PRIO_RE.search(rest)
        if prio:
            out["priority"] = prio.group(1)
            rest = _PRIO_RE.sub("", rest).strip()
        out["title"] = rest
    elif rest:
        prio = _PRIO_RE.search(rest[:40])
        if prio:
            out["priority"] = prio.group(1)

    if not out["title"]:
        # 原文没题目：先退到 YAML 名，再退到第一条步骤，都没有就留空
        out["title"] = fallback_title or _first_step_title(body)
    return out


def yaml_title(yaml_name: str) -> str:
    """`联想日历_167_首次进入课程表引导与空态.yaml` -> `首次进入课程表引导与空态`。

    原文没题目时拿它补 —— YAML 是拆解时人工起的名字，比空着强。
    """
    stem = Path(yaml_name).stem
    parts = stem.split("_", 2)
    return parts[2] if len(parts) >= 3 else ""


def scan(dirs: list[Path],
         yaml_titles: dict[str, str] | None = None) -> list[dict[str, str]]:
    """扫目录，返回每条原始用例的解析结果。同编号后者不覆盖前者。

    目录顺序有意义：先扫到的赢。`cal_cases` 排在前面是因为那是主库，
    `cases` 是后来那批课程表的补充。

    yaml_titles：编号 -> YAML 名里的题目。原文没题目时优先用它 ——
    那是拆解时人给的名字，比从步骤里抠的更准。
    """
    titles = yaml_titles or {}
    seen: dict[str, dict[str, str]] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.txt")):
            m = _FILE_RE.match(f.stem)
            if not m:
                continue        # 命名不合约定就不猜，跳过
            key = m.group(2)
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if key in seen:
                continue
            meta = parse_raw(text, fallback_title=titles.get(key, ""))
            seen[key] = {
                "case_key": key,
                "raw_text": text.strip(),
                "source_path": str(f),
                "title": meta["title"],
                "priority": meta["priority"],
            }
    return list(seen.values())


def yaml_index(cases_dir: Path) -> dict[str, str]:
    """编号 -> YAML 文件名。一个编号有多份时取文件名最短的（最像正主）。"""
    out: dict[str, list[str]] = {}
    if not cases_dir.is_dir():
        return {}
    for f in sorted(cases_dir.glob("*.yaml")):
        if f.name.startswith("_"):
            continue            # _test_invalid.yaml 这类是校验夹具，不是真用例
        key = store.case_number(f.name)
        if key:
            out.setdefault(key, []).append(f.name)
    return {k: sorted(v, key=len)[0] for k, v in out.items()}


def import_all(*, db: str | Path | None = None,
               dirs: list[Path] | None = None,
               cases_dir: Path | None = None,
               overwrite: bool = False) -> dict[str, Any]:
    """导入原始用例 + 关联 YAML。幂等，可以反复跑。

    ## 为什么默认**不覆盖**已存在的用例

    导入之后原文归数据库管 —— 用户可能在网页上改过。再点一次"同步"就把
    改动冲掉，等于白改，而且无从察觉。所以默认只补库里没有的。

    真要拿文件夹的版本盖掉库里的，显式传 overwrite=True（前端要点两下确认）。

    返回统计，给前端和日志看。**不静默吞掉"一条都没扫到"** ——
    目录配错时最怕的就是"看起来成功了，其实库是空的"。
    """
    from .paths import cases_dir as _cases_dir
    from .paths import raw_case_dirs

    dirs = dirs if dirs is not None else raw_case_dirs()
    cases_dir = cases_dir if cases_dir is not None else _cases_dir()

    # ★ YAML 索引要先算：原文没题目时，YAML 里的题目是最优先的兜底
    yidx = yaml_index(cases_dir)
    titles = {k: yaml_title(v) for k, v in yidx.items()}

    rows = scan(dirs, titles)
    have = {c["case_key"] for c in store.list_cases(db=db)}
    created = updated = skipped = 0
    for r in rows:
        if r["case_key"] in have and not overwrite:
            skipped += 1
            continue
        res = store.save_case(
            r["case_key"], title=r["title"] or "", priority=r["priority"],
            raw_text=r["raw_text"], source_path=r["source_path"], db=db)
        if res == "created":
            created += 1
        elif res == "updated":
            updated += 1
        else:
            skipped += 1

    # ── 关联 YAML ──
    linked = 0
    for key, yname in yidx.items():
        if store.set_case_yaml(key, yname, db=db):
            linked += 1
    # 反向：YAML 没了就解除关联（别留一个指向不存在文件的链接）
    unlinked = 0
    for c in store.list_cases(db=db):
        yn = c.get("yaml_name")
        if yn and not (cases_dir / yn).is_file():
            store.set_case_yaml(c["case_key"], None, db=db)
            unlinked += 1

    # 库里已有、但这次没扫到的用例（文件夹里删了 / 编号改了）——
    # **不自动删行**：原文已经在库里，可能是用户后来在网页上改过的唯一副本。
    # 只报出来，让人自己决定。
    scanned = {r["case_key"] for r in rows}
    orphans = [c["case_key"] for c in store.list_cases(db=db)
               if c["case_key"] not in scanned]

    return {
        "scanned_dirs": [str(d) for d in dirs],
        "cases_dir": str(cases_dir),
        "files": len(rows),
        "created": created, "updated": updated, "skipped": skipped,
        "yaml_total": len(list(cases_dir.glob("*.yaml"))) if cases_dir.is_dir() else 0,
        "linked": linked, "unlinked": unlinked,
        "not_in_folder": orphans,
        "total_in_db": len(store.list_cases(db=db)),
    }
