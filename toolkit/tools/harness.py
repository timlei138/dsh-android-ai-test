#!/usr/bin/env python3
"""问出「本会话的工作区在哪、这套东西能不能落盘」——只用标准库。

为什么单独有它
--------------
`runtime.py` 判 storage 根靠 `Path.cwd()`：工具层在 cwd 之下就落 `<target>/storage`，
否则落 `<cwd>/storage`。这在**正常首跑**时是对的（harness 起 bash 就落在工作区），
但有两个时刻它答不了：

  1. 安装期 —— 那时连 venv 都没有，`doctor.py` 跑不起来（它 import 第三方包）；
  2. 有人 `cd` 走了 —— cwd 可能停在工具层任何子目录（实测有会话被切到 `assets/cases/...`），
     这时候 cwd 完全不代表工作区，判出来的规则也就不可信。

所以环境事实要能**独立于 cwd** 拿到。实测 harness 导出的变量只有这些
（dsh 包全文 grep `DSH_[A-Z_]+`，没有 `DSH_WORKSPACE`，也没有任何含
WORK/CWD/SPACE 的名字）：

    DSH_HOME  DSH_PROFILE  DSH_PROFILE_DIR  DSH_SESSION_ID  DSH_SHELL  DSH_WEB_URL

工作区本身藏在**落盘的会话目录名**里：

    $DSH_HOME/sessions/<工作区路径的 slug>/session-$DSH_SESSION_ID/

slug 的规则是「`/` 和 `-` 都折成 `-`，两端再补 `-`」，实测
`--home-alice-work--` == `/home/alice/work`。因为 `-` 被折叠了，
反推**不唯一**（`home-alice-my-work` 既可能是 `/home/alice/my/work`
也可能是 `/home/alice/my-work`）—— 本模块用「逐级 stat，只留在磁盘上真存在
的那条」来消歧，比猜规则可靠。

用法
----
    python3 tools/harness.py           # 人读
    python3 tools/harness.py --json    # 程序读（install.py / doctor.py / CI 都吃这个）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# harness 注入的变量名单（实测）。写死在这儿，是为了以后它改名时
# `available_vars()` 能立刻暴露出来，而不是让某个 `os.environ.get("DSH_XXX")`
# 静默返回 None。
ENV_VARS = ("DSH_HOME", "DSH_PROFILE", "DSH_PROFILE_DIR", "DSH_SESSION_ID",
            "DSH_SHELL", "DSH_WEB_URL", "DSH_AGENTS_HOME", "DSH_WORKSPACE")


def available_vars() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_VARS if os.environ.get(k)}


def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def _isdir(p: str, cache: dict[str, bool]) -> bool:
    if p not in cache:
        try:
            cache[p] = os.path.isdir(p)
        except OSError:      # 权限都不给看时也当"不存在"，别把探测本身弄崩
            cache[p] = False
    return cache[p]


def deslug(slug: str, *, exists=_isdir, home: str | None = None) -> list[str]:
    """把会话目录名还原成候选工作区路径，**只返回磁盘上真存在的目录**。

    slug 折叠了 `/` 与 `-` 的差别，所以一个 slug 对应多条路径；用文件系统
    存在性来消歧。返回按「斜杠最少」（即猜测最保守）排序的候选列表。
    """
    cache: dict[str, bool] = {}
    s = slug.strip()
    absolute = s.startswith("-")
    core = s.strip("-").replace("--", "-")
    if not core:
        return []
    parts = core.split("-")

    # 深度优先合并相邻片段：parts 之间的每个边界要么还是 `-`，要么其实是 `/`
    results: list[tuple[int, str]] = []
    def finish(acc: str) -> str:
        # ⚠️ 绝对路径必须**带着前导 /** 去 stat：早先在这里存了相对形式
        #    （home/xuyl/dsh-work），stat 拿 cwd 拼出来的当然是另一回事，
        #    结果就是"明明存在却找不到对应目录"。
        if absolute:
            return "/" + acc
        return os.path.join(home or ".", acc)

    def walk(i: int, acc: str, slashes: int) -> None:
        if i == len(parts):
            cand = finish(acc)
            if exists(cand, cache):
                results.append((slashes, cand))
            return
        seg = parts[i]
        if not seg:
            walk(i + 1, acc, slashes)
            return
        walk(i + 1, f"{acc}-{seg}" if acc else seg, slashes)          # 边界是 '-'
        walk(i + 1, f"{acc}/{seg}" if acc else seg, slashes + 1)      # 边界是 '/'

    walk(0, "", 0)
    results.sort(key=lambda t: (t[0], t[1]))
    out = [os.path.normpath(p) for _, p in results]
    # 去重保序
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def sessions_dir() -> Path:
    return dsh_home() / "sessions"


def workspace_from_sessions() -> tuple[str | None, str, list[str]]:
    """靠 DSH_SESSION_ID 找会话目录 → 反推工作区。

    返回 (workspace|None, source, candidates)；source 说明判定依据，
    candidates 是同名歧义没消掉时的全部候选（>1 时 workspace 取最保守那条）。
    """
    sid = os.environ.get("DSH_SESSION_ID")
    root = sessions_dir()
    if not sid:
        return None, "无 DSH_SESSION_ID", []
    if not root.is_dir():
        return None, f"会话根目录不存在：{root}", []
    # 实测 DSH_SESSION_ID 的值**本身就带** `session-` 前缀，会话目录名与它
    # 逐字相同（sessions/<工作区slug>/session-3a95899a-…）。两种拼法都试。
    names = [sid] if sid.startswith("session-") else ["session-" + sid, sid]
    hits: list[Path] = []
    try:
        for slug_dir in sorted(root.iterdir()):
            if any((slug_dir / n).is_dir() for n in names):
                hits.append(slug_dir)
    except OSError as e:                       # 读不了 ~/.dsh 也不该崩
        return None, f"读会话根目录失败：{e}", []
    if not hits:
        return None, f"{root} 下没有 {' 或 '.join(names)}", []
    if len(hits) > 1:                          # 理论上不该发生（同一 id 两个工作区）
        pass
    slug = hits[0].name
    cands = deslug(slug)
    if not cands:
        return None, f"slug {slug} 在磁盘上找不到对应目录", []
    src = f"会话目录 {hits[0]}" if len(cands) == 1 else \
          f"会话目录 {hits[0]}（slug 歧义，取斜杠最少的一条）"
    return cands[0], src, cands


def workspace() -> dict:
    """工作区：① DSH_WORKSPACE（若哪天真导出了）② 会话目录 ③ cwd 兜底。"""
    env_ws = (os.environ.get("DSH_WORKSPACE") or "").strip()
    if env_ws and Path(env_ws).is_dir():
        return {"path": str(Path(env_ws).resolve()), "source": "env:DSH_WORKSPACE",
                "confidence": "high", "candidates": [str(Path(env_ws).resolve())]}
    ws, src, cands = workspace_from_sessions()
    if ws:
        return {"path": ws, "source": src,
                "confidence": "high" if len(cands) <= 1 else "medium",
                "candidates": cands}
    cwd = str(Path.cwd().resolve())
    return {"path": cwd, "source": f"兜底用 cwd（{src}）", "confidence": "low",
            "candidates": [cwd]}


def probe_writable(p: str | Path) -> tuple[bool, str]:
    """真写一个文件再删。`os.access` 不算数 —— 它不看沙箱实际放行范围。"""
    try:
        d = Path(p)
        d.mkdir(parents=True, exist_ok=True)
        f = d / ".harness_probe"
        f.write_text("x", encoding="utf-8")
        f.unlink()
        return True, "可写"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def report(tool_root: Path | None = None) -> dict:
    tr = Path(tool_root) if tool_root else Path(__file__).resolve().parent.parent
    ws = workspace()
    inside = None
    try:
        inside = os.path.commonpath([str(tr.resolve()), str(Path(ws["path"]).resolve())]) == \
            str(Path(ws["path"]).resolve())
    except (ValueError, OSError):
        inside = None
    probes = {}
    for label, target in (("工作区", ws["path"]),
                          ("工具层", str(tr)),
                          ("工具层/storage", str(tr / "storage")),
                          ("cwd", str(Path.cwd()))):
        ok_, why = probe_writable(target) if target else (False, "路径为空")
        probes[label] = {"path": target, "writable": ok_, "why": why}
    return {
        "harness_vars": available_vars(),
        "workspace": ws,
        "tool_root": str(tr),
        "tool_root_in_workspace": inside,
        "writability": probes,
        "storage_rule": "工具层在工作区之内 -> 产物跟着工具层走，不需要任何环境变量",
        "verdict": "ok" if inside and probes["工具层/storage"]["writable"] else "check",
    }


def is_inside(path: str | Path, ws: dict) -> bool | None:
    """path 在工作区之内吗。判不了工作区时返回 None（调用方别拿 False 当结论）。"""
    try:
        target = Path(path).resolve()
        base = Path(ws["path"]).resolve()
    except OSError:
        return None
    if target == base:
        return True
    try:
        return base in target.parents
    except (ValueError, OSError):        # 跨盘符（Windows）等
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="探测 harness 会话环境（纯标准库，装依赖前就能跑）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tool-root", default=None)
    ap.add_argument("--inside", metavar="PATH", default=None,
                    help="只回答一件事：PATH 在不在工作区内。0=在内 1=在外 2=判不了。"
                         "给 install.py / doctor 当查询口用，别自己解析 --json")
    a = ap.parse_args()

    if a.inside:
        ws = workspace()
        # 判不出工作区时（confidence=low，只能拿 cwd 兜底）**不要**给"在内/在外"的
        # 结论 —— 那会让调用方把一次巧合当规则。返回 2，让它自己决定怎么问人。
        verdict = is_inside(a.inside, ws) if ws["confidence"] != "low" else None
        tag = {True: "在内", False: "在外", None: "判不了"}[verdict]
        print(f"{a.inside} {tag} 工作区 {ws['path']}  [{ws['confidence']}]  依据：{ws['source']}")
        return {True: 0, False: 1, None: 2}[verdict]
    r = report(Path(a.tool_root) if a.tool_root else None)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        ws = r["workspace"]
        print(f"工作区    {ws['path']}   [{ws['confidence']}]")
        print(f"  依据    {ws['source']}")
        if len(ws["candidates"]) > 1:
            print(f"  歧义    {'  '.join(ws['candidates'])}")
        print(f"工具层    {r['tool_root']}")
        print(f"  在工作区内 {r['tool_root_in_workspace']}")
        print("导出变量  " + (", ".join(r["harness_vars"]) or "（一个都没有 —— 不在 harness 会话里）"))
        for label, p in r["writability"].items():
            print(f"  {'✓' if p['writable'] else '✗'} {label:<16} {p['path']}  {p['why']}")
        print(f"结论      {r['verdict']}   {r['storage_rule']}")
    return 0 if r["verdict"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
