"""cleanup：证据与日志轮转，防 storage 无限膨胀。

每步 act/observe 都落全分辨率截图，长期不用必爆——定期跑或挂计划任务。
按**文件**自身 mtime 判断（目录 mtime 只在增删子项时变，按目录判会误删
正在写入的活跃证据）；当前会话的证据目录整目录跳过。

⚠️ **别手工 `Remove-Item storage/evidence/<目录>`** —— 数据库里的事件仍引用那些
图，删了之后记录页会全是空白（真实事故：清理临时目录时连已入库会话的证据一起删了）。
要删请走 `--rm-dir`，它会**先查数据库引用**，被引用就拒绝。

用法：
  python cleanup.py --days 7              # 删 7 天前的 evidence/ 与 logs/
  python cleanup.py --days 7 --dry        # 只报告将删什么
  python cleanup.py --rm-dir t186         # 删某个证据子目录（有引用则拒绝）
  python cleanup.py --rm-dir t186 --force # 明知有引用还要删
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
import sqlite3
from runtime import STORAGE_DIR
from common import rel

ROOT = Path(__file__).resolve().parent.parent


def _db_referenced(rel_prefix: str) -> list[str]:
    """返回数据库中引用了该路径前缀的会话标识（如 `#275`）。"""
    from db import get_db
    prefix = Path(rel_prefix)
    prefix = (prefix if prefix.is_absolute() else ROOT / prefix).resolve()
    rows = get_db()._conn().execute(
        "SELECT session_id, evidence FROM events WHERE evidence IS NOT NULL").fetchall()
    users = set()
    for row in rows:
        evidence = Path(row["evidence"])
        evidence = (evidence if evidence.is_absolute() else ROOT / evidence).resolve()
        if evidence == prefix or evidence.is_relative_to(prefix):
            users.add(row["session_id"])
    return [f"#{sid}" for sid in sorted(users)]


def rm_dir(name: str, force: bool = False) -> int:
    """删 storage/evidence/<name>，**先查数据库引用**。返回退出码。"""
    target = (STORAGE_DIR / "evidence" / name).resolve()
    root = (STORAGE_DIR / "evidence").resolve()
    if not target.is_relative_to(root) or target == root:
        print(f"拒绝：{name} 不是 evidence 下的子目录")
        return 1
    if not target.is_dir():
        print(f"不存在：{target}")
        return 1

    users = _db_referenced(str(target))
    if users and not force:
        print(f"拒绝删除 storage/evidence/{name} —— 数据库里这些会话还引用着它："
              f"{', '.join(users)}")
        print("  删了记录页的截图会全部空白。确实要删请加 --force"
              "（或先删掉那些会话记录）。")
        return 2
    if users:
        print(f"⚠️ --force：{name} 仍被 {', '.join(users)} 引用，强行删除")

    n = 0
    for f in sorted(target.rglob("*"), reverse=True):
        if f.is_file():
            f.unlink(missing_ok=True)
            n += 1
        elif f.is_dir():
            f.rmdir()
    target.rmdir()
    print(f"已删除 storage/evidence/{name}（{n} 个文件）")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="清理过期证据/日志")
    p.add_argument("--days", type=int, default=7, help="保留最近 N 天，默认 7")
    p.add_argument("--dry", action="store_true", help="只报告不删除")
    p.add_argument("--rm-dir", default=None,
                   help="删 storage/evidence/<名字> 子目录（被数据库引用则拒绝）")
    p.add_argument("--force", action="store_true", help="配合 --rm-dir：明知有引用也删")
    args = p.parse_args()

    if args.rm_dir:
        raise SystemExit(rm_dir(args.rm_dir, args.force))

    cutoff = time.time() - args.days * 86400
    try:
        from db import current_session
        cur = current_session()
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot verify current session before cleanup: {error}") from error

    removed = kept = 0
    for name in ("evidence", "logs"):
        root = STORAGE_DIR / name
        if not root.is_dir():
            continue
        # 当前会话的证据目录整目录保护（跑一半被清掉=取证断链）
        protected = root / str(cur) if cur else None
        for f in list(root.rglob("*")):
            if not f.is_file():
                continue
            if protected and f.is_relative_to(protected):
                kept += 1
                continue
            if f.stat().st_mtime >= cutoff:
                kept += 1
                continue
            # ⚠️ 还被数据库引用着的证据**不删** —— 删了记录页就空白
            if _db_referenced(rel(f)):
                kept += 1
                continue
            if args.dry:
                removed += 1
                print(f"[dry] 将删 {f}")
                continue
            f.unlink(missing_ok=True)
            removed += 1
        # 顺手剪掉空目录（不含当前会话目录）
        if not args.dry:
            for d in sorted(root.rglob("*"), reverse=True):
                if d.is_dir() and d != protected and not any(d.iterdir()):
                    d.rmdir()
    print(f"清理完成：删除 {removed} 个文件，保留 {kept} 个（最近 {args.days} 天"
          + (f"；当前会话 {cur} 目录已保护" if cur else "") + "）")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Cleanup refused/failed: {error}")
        raise SystemExit(1)
