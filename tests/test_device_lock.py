# -*- coding: utf-8 -*-
"""设备互斥锁 —— 同一台设备不能并发跑两条用例。

为什么要有这个测试（2026-10 实测事故）：

    用户派了两个子 Agent 同时跑用例，两个 `run_case_json.py` 抢 `<设备序列号>`：

      report_15  FAIL     起点不符：期望 TimetableActivity，实际 AllInOneActivity
      report_16  BLOCKED  连点 (2697,371) 无效，判卡死
      report_17  (无)     连点 btnCreateManually 两次没反应

    **没有一条是被测 App 的问题。**全是两条用例在抢设备：
    一条点了「课程表」，另一条看到的就是课程表；
    另一条退回日历首页，这一条就找不到自己的周视图了。

    最恶劣的是它们**不报冲突**，只产出一份看起来正常的错误报告。

所以这里的断言围绕三点：
    ① 同一序列号 -> 串行（第二个必须等）
    ② 不同序列号 -> 可并行（两设备不该互相阻塞）
    ③ 进程崩溃 -> 锁自动释放（不能留死锁）
"""
import os
import subprocess
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


from android_agent.device.lock import DeviceLock, DeviceBusy, _lock_path

PY = sys.executable
HERE = str(Path(__file__).resolve().parents[1])

print("=" * 74)
print("1) 同一台设备：第二个必须等（核心）")
print("=" * 74)

# 子进程：拿到锁 -> 标记 -> 睡 N 秒 -> 释放
# ⚠️ 这是 .format() 模板，必须用 {here} 占位符，不能直接写 HERE ——
#    写 HERE 的话子进程里就是一个未定义的名字。
HOLD = r'''
import sys, time
sys.path.insert(0, r"{here}\src")
from android_agent.device.lock import DeviceLock
with DeviceLock("{serial}", max_wait_s=30, verbose=True) as lk:
    open(r"{flag}", "w").write("held")
    time.sleep({sec})
'''


def spawn_holder(serial, sec, flag):
    code = HOLD.format(here=HERE, serial=serial, sec=sec, flag=flag)
    return subprocess.Popen([PY, "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


flag = os.path.join(HERE, "_locktest.flag")
for f in (flag,):
    if os.path.exists(f):
        os.remove(f)

holder = spawn_holder("TESTDEV-1", 6, flag)
# 等它真的拿到锁
for _ in range(60):
    if os.path.exists(flag):
        break
    time.sleep(0.1)
ck("持锁进程已启动", os.path.exists(flag))

t0 = time.time()
lock = DeviceLock("TESTDEV-1", max_wait_s=30, verbose=False)
lock.acquire()
waited = time.time() - t0
lock.release()
holder.wait(timeout=30)

ck("★ 第二个调用者被挡住（等到前一个跑完）", waited >= 3.0, f"等了 {waited:.1f}s")
ck("等待时间合理（≈剩下的 6s 内）", waited < 20, f"{waited:.1f}s")

if os.path.exists(flag):
    os.remove(flag)

print()
print("=" * 74)
print("2) 不同设备：互不阻塞（两台机器应该能并行）")
print("=" * 74)

other = spawn_holder("TESTDEV-OTHER", 5, flag + ".2")
for _ in range(60):
    if os.path.exists(flag + ".2"):
        break
    time.sleep(0.1)

t0 = time.time()
lock2 = DeviceLock("TESTDEV-1", max_wait_s=30, verbose=False)
lock2.acquire()
got = time.time() - t0
lock2.release()
other.wait(timeout=30)

ck("★ 另一台设备的锁不挡本设备", got < 2.0, f"{got:.2f}s")
for f in (flag + ".2",):
    if os.path.exists(f):
        os.remove(f)

print()
print("=" * 74)
print("3) 等太久 -> 明确失败，绝不默默往下跑")
print("=" * 74)

holder = spawn_holder("TESTDEV-BUSY", 6, flag + ".3")
for _ in range(60):
    if os.path.exists(flag + ".3"):
        break
    time.sleep(0.1)

try:
    DeviceLock("TESTDEV-BUSY", max_wait_s=1.0, verbose=False).acquire()
    ck("★ 超时应抛 DeviceBusy", False, "没抛")
except DeviceBusy as e:
    msg = str(e)
    ck("★ 超时抛 DeviceBusy", True)
    ck("说明里点明『不能并发』", "不能并发" in msg, msg.splitlines()[0][:40])
    ck("给了处置建议", "等前一条跑完" in msg)
holder.wait(timeout=30)
if os.path.exists(flag + ".3"):
    os.remove(flag + ".3")

print()
print("=" * 74)
print("4) 进程崩溃 -> 锁自动释放（不能留死锁）")
print("=" * 74)

# 直接 kill 掉持锁进程，锁应被操作系统回收
# ⚠️ 子进程要自己把 src 加进 sys.path —— 不能用外层变量直接内嵌，
#    必须走 f-string 插值（早先写成字面量 HERE，子进程 NameError）。
code = (f'import sys,time; sys.path.insert(0, r"{HERE}\\src"); '
        f'from android_agent.device.lock import DeviceLock; '
        f'l=DeviceLock("TESTDEV-KILL", max_wait_s=5); l.acquire(); '
        f'print("ok", flush=True); time.sleep(60)')
p = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE)
line = p.stdout.readline()
ck("持锁进程已就绪", b"ok" in line, line[:20])
p.kill()
p.wait(timeout=10)
time.sleep(0.5)

t0 = time.time()
try:
    lk = DeviceLock("TESTDEV-KILL", max_wait_s=3, verbose=False)
    lk.acquire()
    lk.release()
    ck("★ 崩溃后锁可重新获取（无死锁）", True, f"{time.time()-t0:.2f}s")
except DeviceBusy:
    ck("★ 崩溃后锁可重新获取（无死锁）", False, "仍被占 -> 死锁")

print()
print("=" * 74)
print("5) 锁真的接进了 run_case_json（不能只有模块没人用）")
print("=" * 74)
import io

src = io.open(os.path.join(HERE, "run_case_json.py"), encoding="utf-8").read()
ck("导入了 DeviceLock", "from android_agent.device.lock import DeviceLock" in src)
# 位置判据：acquire 必须出现在 **调用** 执行器之前。
# （不能用 `_run_locked(` —— 函数定义在文件靠前，比 acquire 还早。）
_call = src.index("report = _run_locked(")
ck("★ 在真正执行用例之前 acquire", src.index("_lock.acquire()") < _call)
ck("★ 执行段在 try 内（配合 finally 释放）",
   src.index("        report = _run_locked(") > src.index("    try:"))
ck("★ 用 finally 释放（异常也不会漏）", "finally:" in src and "_lock.release()" in src)
ck("拿不到锁返回 BLOCKED", "BLOCKED" in src and "另一条用例占用" in src)

print()
print("=" * 74)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 74)
sys.exit(1 if FAIL else 0)
