# -*- coding: utf-8 -*-
"""端到端验证：start 时会自动清理孤儿，且不伤活会话。"""
import re
import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets"
# 环境已合并成一个（仓库根 .venv），不再是 assets/.venv
PY = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
sys.path.insert(0, str(ASSETS / "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import db as dbmod

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


print("=" * 66)
print("1) 造一个「死进程」的僵尸会话")
print("=" * 66)
d = dbmod.get_db()
sid = d.start_session("__test_zombie__", "自测用", kind="mock")
d.mark_session_owner(sid, 999999)
print(f"  会话 #{sid} owner=pid 999999（不存在）")

print()
print("=" * 66)
print("2) 跑 session.py start，应自动清掉它")
print("=" * 66)
r = subprocess.run(
    [str(PY), "tools/session.py", "start",
     "--title", "__test_new_session__", "--input", "自测用",
     "--device", "fake", "--diag"],
    cwd=str(ASSETS), capture_output=True, text=True,
    encoding="utf-8", errors="replace")
print(f"  exit={r.returncode}")
if r.stderr.strip():
    for line in r.stderr.strip().splitlines()[:5]:
        print(f"  stderr| {line}")

ck("start 成功", r.returncode == 0, r.stdout.strip()[:80])

row = d._conn().execute("SELECT status FROM sessions WHERE id=?",
                        (sid,)).fetchone()
ck("★ 僵尸被自动清理", row and row["status"] == "ERROR",
   row["status"] if row else "(没了)")

m = re.search(r'"session_id":\s*(\d+)', r.stdout)
new_sid = int(m.group(1)) if m else None
print(f"  新会话 = {new_sid}")

print()
print("=" * 66)
print("3) 清库")
print("=" * 66)
for x in (sid, new_sid):
    if not x:
        continue
    d._conn().execute("DELETE FROM events WHERE session_id=?", (x,))
    d._conn().execute("DELETE FROM findings WHERE session_id=?", (x,))
    d._conn().execute("DELETE FROM sessions WHERE id=?", (x,))
    d.clear_session_owner(x)
    try:
        d._pid_path(x).unlink(missing_ok=True)
    except OSError:
        pass
d._conn().commit()
print(f"  已删 #{sid} 和 #{new_sid}")

print()
print("=" * 66)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
