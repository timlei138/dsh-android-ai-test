"""临时文件的唯一去处，以及自动清理。

## 为什么要这个模块

以前临时文件散在仓库根目录：`_xxx.json`、`report_*.json`、`run_*.log`、
还有 7 个 `_tmp_*` 目录。后果是**没人分得清哪些是有价值的产物、
哪些是可以删的垃圾**，于是只能全部留着，仓库越来越乱。

现在所有临时产物都写 `tmp/`，规则只有一条：

    tmp/ 下的东西随时可删，超过 24 小时自动删。

## 安全边界

清理只删 **tmp/ 目录内部、且超过时限的文件**。三重保护：

1. 目标必须在 `tmp_dir()` 目录之内（按 resolve 后的真实路径判断）
2. 只删文件，不删目录
3. 遇到任何异常都跳过那一个文件，绝不因为清理失败而中断主流程

第 1 条是防路径穿越的关键：调用方传进来的永远应该是 tmp 下的路径，
但万一哪天有人传了 `../assets`，这里必须挡住，而不是把资产删了。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# tmp 目录名（仓库根下的 tmp/）
TMP_NAME = "tmp"

# 默认保留时长：1 天。用户要求"自动清理超过一天的临时文件"。
DEFAULT_MAX_AGE_H = 24.0


def repo_root() -> Path:
    """仓库根目录。

    ⚠️ 用 reports.py 自己的位置往上数：src/android_agent/reports/ -> 根
       少退一层就会指到 src/，然后 tmp/ 建在 src 里，越跑越乱。
    """
    return Path(__file__).resolve().parents[3]


def tmp_dir(*, create: bool = True) -> Path:
    """返回 tmp/ 目录，默认顺手创建。

    统一从这里取路径 —— 别在别处写 `Path("tmp")`，
    那会跟着当前工作目录跑（在子进程里 cwd 可能不是仓库根）。
    """
    p = repo_root() / TMP_NAME
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def is_inside_tmp(p: str | Path) -> bool:
    """真实路径是否落在 tmp/ 内部（防路径穿越）。

    必须用 resolve()：`tmp/../../assets` 这种字面上以 tmp 开头的路径，
    解析后其实在外面。
    """
    try:
        target = Path(p).resolve()
        root = tmp_dir(create=False).resolve()
    except OSError:
        return False
    return target == root or root in target.parents


def clean_tmp(max_age_hours: float = DEFAULT_MAX_AGE_H,
              *, dry_run: bool = False, verbose: bool = False) -> dict:
    """删掉 tmp/ 下超过 max_age_hours 的文件。

    返回统计：{"removed": [...], "kept": n, "bytes": n, "errors": [...]}

    ⚠️ 不删目录本身，也不删目录里的 .gitkeep ——
       否则 tmp/ 会在清理后消失，下次写文件时又要重新 mkdir，
       更糟的是某些流程会因此报"目录不存在"。

    ⚠️ 单个文件删不掉（被占用等）只记进 errors，**不抛异常**。
       清理是顺便做的事，不该让主流程因为一个删不掉的旧日志而失败。
    """
    root = tmp_dir(create=False)
    if not root.is_dir():
        return {"removed": [], "kept": 0, "bytes": 0, "errors": []}

    cutoff = time.time() - max_age_hours * 3600.0
    removed: list[str] = []
    errors: list[str] = []
    kept = 0
    freed = 0

    for p in root.rglob("*"):
        # 只处理文件；目录留给它里面的文件被删空后自然存在
        if not p.is_file():
            continue
        # 保住占位文件，理由见 docstring
        if p.name == ".gitkeep":
            continue
        try:
            st = p.stat()
        except OSError as exc:
            errors.append(f"{p.name}: stat 失败（{exc}）")
            continue
        if st.st_mtime >= cutoff:
            kept += 1
            continue
        # 二次确认在 tmp 内 —— 防 symlink 指到外面
        if not is_inside_tmp(p):
            errors.append(f"{p.name}: 不在 tmp 内，跳过")
            continue
        if dry_run:
            removed.append(str(p.relative_to(root)))
            freed += st.st_size
            continue
        try:
            size = st.st_size
            p.unlink()
            removed.append(str(p.relative_to(root)))
            freed += size
            if verbose:
                print(f"  🗑 tmp/{p.relative_to(root)}")
        except OSError as exc:
            errors.append(f"{p.name}: 删除失败（{exc}）")

    # 顺手清掉空目录（但不删 tmp 本身）
    if not dry_run:
        for d in sorted(root.rglob("*"), key=lambda x: len(x.parts),
                        reverse=True):
            if d.is_dir() and d != root:
                try:
                    if not any(d.iterdir()):
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass

    return {"removed": removed, "kept": kept, "bytes": freed,
            "errors": errors}


def clean(repo_scratch: bool = False,
          max_age_hours: float = DEFAULT_MAX_AGE_H, **kw) -> dict:
    """主入口：清 tmp/。

    `repo_scratch=True` 时**额外**清理仓库根目录里遗留的老式临时文件
    （`_*.json`/`_*.py`/`_*.mjs`、`report_*.json`、`run_*.log`、`_tmp_*` 目录）。
    这是给"从旧结构迁移过来"用的，日常运行不需要开。

    ⚠️ 即使开了这个开关，也只删**明确匹配那些模式**的东西，
       绝不按目录整体删除 —— 仓库根里有源码和用例资产。
    """
    result = clean_tmp(max_age_hours=max_age_hours, **kw)
    if not repo_scratch:
        return result

    root = repo_root()
    legacy = _legacy_scratch(root)
    cutoff = time.time() - max_age_hours * 3600.0
    for p in legacy:
        try:
            if p.stat().st_mtime >= cutoff:
                result["kept"] += 1
                continue
            if not kw.get("dry_run"):
                size = p.stat().st_size if p.is_file() else 0
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                result["bytes"] += size
            result["removed"].append(f"<repo>/{p.name}")
        except OSError as exc:
            result["errors"].append(f"{p.name}: {exc}")
    return result


def _legacy_scratch(root: Path) -> list[Path]:
    """仓库根下的老式临时产物（迁移期兼容，日常不该再有）。

    ⚠️ 只匹配**明确的产物模式**，绝不按目录整删：
       · `_*.json` / `_*.py` / `_*.mjs`  —— 调试脚本与测试输出
       · `report_*.json` / `run_*.log`  —— 运行报告与日志
       · `child_*.db`                   —— LangGraph 的 checkpoint 库
       · `_tmp_*` / `_iso*` 目录         —— 各测试的工作区

       `task_*.json` / `cases_to_run.json` 这类是**输入**，不在此列 ——
       删了用例就跑不起来了。
    """
    out: list[Path] = []
    patterns = ("_*.json", "_*.py", "_*.mjs", "report_*.json", "run_*.log",
                "child_*.db")
    for pat in patterns:
        for p in root.glob(pat):
            # 别把下划线开头的正式模块误删（目前没有，但留着防线）
            if p.name in ("__init__.py", "_version.py"):
                continue
            out.append(p)
    for d in root.glob("_tmp_*"):
        if d.is_dir():
            out.append(d)
    for d in root.glob("_iso*"):
        if d.is_dir():
            out.append(d)
    return out


if __name__ == "__main__":  # 手动清理：python -m android_agent.reports.scratch
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="清理 tmp/ 下的过期临时文件")
    ap.add_argument("--hours", type=float, default=DEFAULT_MAX_AGE_H,
                    help="保留多少小时以内的文件（默认 24）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不删")
    ap.add_argument("--legacy", action="store_true",
                    help="连仓库根下的老式临时文件一起清（迁移用）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    r = clean(repo_scratch=args.legacy, max_age_hours=args.hours,
              dry_run=args.dry_run, verbose=args.verbose)
    n = len(r["removed"])
    verb = "将删除" if args.dry_run else "已删除"
    print(f"{verb} {n} 个文件，释放 {r['bytes'] / 1024:.1f} KB；"
          f"保留 {r['kept']} 个未过期文件")
    if r["errors"]:
        print("跳过：", file=sys.stderr)
        for e in r["errors"]:
            print(f"  - {e}", file=sys.stderr)
