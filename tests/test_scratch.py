"""tmp 目录与自动清理：验证"该删的删掉、不该删的一个都不能动"。

## 为什么这个测试的重点是"不删"

清理逻辑的风险是**不对称**的：
  · 该删的没删 —— 只是磁盘脏一点，无害
  · 不该删的删了 —— 资产/源码/用例没了，事故

所以这里的用例大部分在证明"这个不该被删"。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.report import scratch  # noqa: E402

PASS = FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def age_file(p: Path, hours: float) -> None:
    """把文件的 mtime 往前推 hours 小时。"""
    t = time.time() - hours * 3600.0
    os.utime(p, (t, t))


print("=" * 72)
print("1) 路径：tmp 必须在仓库根，不能跑到 src 里去")
print("=" * 72)
check("repo_root 是仓库根", scratch.repo_root() == ROOT,
      str(scratch.repo_root()))
check("tmp 在仓库根下", scratch.tmp_dir() == ROOT / "tmp",
      str(scratch.tmp_dir()))

print()
print("=" * 72)
print("2) 核心：超过 24h 的删掉，没超过的留着")
print("=" * 72)
tmp = scratch.tmp_dir()
old_file = tmp / "_test_old.txt"
new_file = tmp / "_test_new.txt"
old_file.write_text("old", encoding="utf-8")
new_file.write_text("new", encoding="utf-8")
age_file(old_file, 30)          # 30 小时 > 24
age_file(new_file, 1)           # 1 小时

r = scratch.clean_tmp(max_age_hours=24)
check("过期文件被删", not old_file.exists(), "已不存在")
check("未过期文件保留", new_file.exists(), "仍存在")
check("保留计数正确", r["kept"] >= 1, f"kept={r['kept']}")
check("删除清单里有它", any("_test_old" in x for x in r["removed"]),
      str(r["removed"]))
new_file.unlink()

print()
print("=" * 72)
print("3) 边界：阈值可以调（1 小时）")
print("=" * 72)
f2 = tmp / "_test_2h.txt"
f2.write_text("x", encoding="utf-8")
age_file(f2, 2)                 # 2 小时：24h 阈值下该留，1h 阈值下该删
r24 = scratch.clean_tmp(max_age_hours=24, dry_run=True)
check("24h 阈值下保留", "_test_2h.txt" not in " ".join(r24["removed"]),
      str(r24["removed"]))
r1 = scratch.clean_tmp(max_age_hours=1, dry_run=True)
check("1h 阈值下该删", "_test_2h.txt" in " ".join(r1["removed"]),
      str(r1["removed"]))
f2.unlink()

print()
print("=" * 72)
print("4) ★安全：绝不能删到 tmp 外面去")
print("=" * 72)
# 构造一个指向外面的路径
outside = ROOT / "assets" / "storage" / "cases"
check("tmp 内的路径判定为 True",
      scratch.is_inside_tmp(tmp / "x.json"), "")
check("仓库根的路径判定为 False",
      not scratch.is_inside_tmp(ROOT / "run_case_json.py"), "")
check("assets 判定为 False",
      not scratch.is_inside_tmp(outside), "")
# 字面以 tmp 开头、实际跳出去的路径
check("路径穿越被识破",
      not scratch.is_inside_tmp(tmp / ".." / "assets"), "tmp/../assets")

# 真实文件：仓库根的文件、assets 的文件都必须活着
guard = ROOT / "run_case_json.py"
check("入口脚本仍在", guard.exists(), "")

print()
print("=" * 72)
print("5) dry_run 只看不删")
print("=" * 72)
f3 = tmp / "_test_dry.txt"
f3.write_text("x" * 100, encoding="utf-8")
age_file(f3, 48)
rd = scratch.clean_tmp(max_age_hours=24, dry_run=True)
check("dry_run 报告了它", "_test_dry.txt" in " ".join(rd["removed"]),
      str(rd["removed"]))
check("dry_run 没有真的删", f3.exists(), "仍存在")
check("dry_run 统计了字节", rd["bytes"] >= 100, f"{rd['bytes']} B")
r3 = scratch.clean_tmp(max_age_hours=24)
check("正式跑时删掉", not f3.exists(), "")

print()
print("=" * 72)
print("6) 目录结构：tmp 本身不能被删掉")
print("=" * 72)
f4 = tmp / "_test_dir.txt"
f4.write_text("x", encoding="utf-8")
age_file(f4, 48)
scratch.clean_tmp(max_age_hours=24)
check("tmp 目录本身还在", tmp.is_dir(), str(tmp))

print()
print("=" * 72)
print("7) .gitkeep 要保住（占位文件不该被清走）")
print("=" * 72)
gk = tmp / ".gitkeep"
gk.write_text("", encoding="utf-8")
age_file(gk, 999)
scratch.clean_tmp(max_age_hours=24)
check(".gitkeep 保留", gk.exists(), "")

print()
print("=" * 72)
print("8) 异常不致命：清不掉也要正常返回")
print("=" * 72)
r = scratch.clean_tmp(max_age_hours=24)
check("返回结构完整",
      all(k in r for k in ("removed", "kept", "bytes", "errors")),
      str(list(r.keys())))
check("errors 是列表", isinstance(r["errors"], list), "")

print()
if FAIL:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
    raise SystemExit(1)
print(f"全部通过（{PASS} 项）")
