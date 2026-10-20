# -*- coding: utf-8 -*-
"""孤儿会话清理机制的测试。

用户要求："进程被强杀后会话永远停在 running，加个清理机制。"

关键设计约束（必须被测试锁住）：
  1. 判据是 **pid 存活**，不是时间 —— 用户明确说过"执行不应有时间限制"
  2. 只有**能证明**是孤儿的才清（有登记 + pid 已死）
  3. 没有登记的 running 会话**不许猜**（可能是别的机器/老版本建的）
  4. 进程还活着的会话**绝不能**被清
  5. 清理失败不能影响开新会话
"""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets"
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
print("1) pid 存活判定")
print("=" * 66)
ck("当前进程 alive", dbmod.SessionDB._pid_alive(os.getpid()))
ck("pid 0 判死", not dbmod.SessionDB._pid_alive(0))
ck("负数判死", not dbmod.SessionDB._pid_alive(-1))
# 999999 这种大概率不存在
ck("不存在的 pid 判死", not dbmod.SessionDB._pid_alive(999999))

print()
print("=" * 66)
print("2) 真起一个子进程，验证能判活 / 杀后判死")
print("=" * 66)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
try:
    time.sleep(0.8)
    ck("子进程活时判活", dbmod.SessionDB._pid_alive(child.pid), f"pid={child.pid}")
    child.kill()
    child.wait(timeout=10)
    time.sleep(0.6)
    ck("杀掉后判死", not dbmod.SessionDB._pid_alive(child.pid))
finally:
    try:
        child.kill()
    except Exception:
        pass

print()
print("=" * 66)
print("3) 真实清理：造一个『已死进程』的 owner 再清理")
print("=" * 66)
d = dbmod.get_db()
sid_alive = d.start_session("__test_alive__", "自测用，可删", kind="mock")
sid_dead = d.start_session("__test_dead__", "自测用，可删", kind="mock")
print(f"  建了两个会话: alive=#{sid_alive} dead=#{sid_dead}")

# sid_alive 保持"本进程"（活着）
# sid_dead 伪造成一个已死的 pid
dead_pid = 999999
d.mark_session_owner(sid_dead, dead_pid)
print(f"  把 #{sid_dead} 的 owner 改成不存在的 pid {dead_pid}")

reaped = d.reap_orphan_sessions()
ids = [r["session_id"] for r in reaped]
ck("清理了 dead 会话", sid_dead in ids, str(ids))
ck("★ 没动活着的会话", sid_alive not in ids, str(ids))

# 检查状态
row = d._conn().execute("SELECT status FROM sessions WHERE id=?",
                        (sid_dead,)).fetchone()
ck("dead 会话变成 ERROR", row["status"] == "ERROR", row["status"])
row2 = d._conn().execute("SELECT status FROM sessions WHERE id=?",
                         (sid_alive,)).fetchone()
ck("alive 会话仍 running", row2["status"] == "running", row2["status"])

print()
print("=" * 66)
print("4) 没有 owner 登记的 running 会话：不许猜")
print("=" * 66)
d._pid_path(sid_alive).unlink(missing_ok=True)
reaped2 = d.reap_orphan_sessions()
ck("无登记的会话没被清理",
   sid_alive not in [r["session_id"] for r in reaped2], str(reaped2))

print()
print("=" * 66)
print("5) 清库：删掉测试用的会话")
print("=" * 66)
for sid in (sid_alive, sid_dead):
    d._conn().execute("DELETE FROM events WHERE session_id=?", (sid,))
    d._conn().execute("DELETE FROM findings WHERE session_id=?", (sid,))
    d._conn().execute("DELETE FROM sessions WHERE id=?", (sid,))
    d.clear_session_owner(sid)
    try:
        d._pid_path(sid).unlink(missing_ok=True)
    except OSError:
        pass
d._conn().commit()
print(f"  已删除 #{sid_alive} 和 #{sid_dead}")

print()
print("=" * 66)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
