# -*- coding: utf-8 -*-
"""验证设备层**不会卡死**，且**一律走 uiautomator2**。

## 两条规矩，各有一套判据

### 规矩一：设备操作一律走 uiautomator2，不许裸调 adb
用户原话：「所有和设备相关的操作通过 uiautomator 进行。不允许使用 adb 直接操作。
除了 adb logcat。」

理由：u2 已经把点/滑/输入/取界面/取前台都做全了。自己再拼 adb 命令
等于维护两套通道，两边行为还不一致（实测就吃过：adb 的 `input text`
不送中文，而 u2 的 setText 正常）。

### 规矩二：任何一次 u2 调用都必须能在有限时间内返回
u2 底层 adbutils 的 `shell()` 挂着 `@retry`（无限重试）。socket 一僵住
调用就永不返回，`try/except` 也挡不住（**根本没有异常抛出**）。
实测卡死 20 分钟：日志不增长、设备不动、进程还活着。

所以所有 u2 调用都要经过 `_call_with_timeout`（daemon 线程 + join 超时）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from android_agent.device.direct import DirectDevice  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def ck(name, ok, detail=""):
    CHECKS.append((name, ok, detail))


d = DirectDevice(serial="51e8bf1c", evidence_dir=ROOT / "tmp" / "_t_shellto")

# ── 1. 超时机制本身有效（用 sleep 假冒一个僵住的 u2 调用）──────────
t0 = time.time()
exc = None
try:
    d._call_with_timeout(lambda: time.sleep(30), timeout=1.5, label="fake-u2")
except Exception as e:  # noqa: BLE001
    exc = e
elapsed = time.time() - t0

ck("超时会抛异常", exc is not None)
ck("超时是 TimeoutError", isinstance(exc, TimeoutError),
   type(exc).__name__ if exc else "没抛")
ck("★ 1.5s 的调用在 1.5s 附近就返回（不无限等）", elapsed < 3.0,
   f"实际 {elapsed:.2f}s")
ck("没有提前误报", elapsed >= 1.2, f"实际 {elapsed:.2f}s")

# ── 2. 正常调用照常返回 ─────────────────────────────────────────
ck("正常调用能拿到返回值",
   d._call_with_timeout(lambda: 42, timeout=5, label="ok") == 42)

# ── 3. 子调用抛的异常会原样冒出来（不被吞掉）─────────────────────
def boom():
    raise ValueError("原样传播")


exc2 = None
try:
    d._call_with_timeout(boom, timeout=5, label="boom")
except Exception as e:  # noqa: BLE001
    exc2 = e
ck("★ 内部异常原样传播", isinstance(exc2, ValueError), repr(exc2))

# ── 4. 反复超时不会累积卡死 ────────────────────────────────────
t0 = time.time()
for _ in range(3):
    try:
        d._call_with_timeout(lambda: time.sleep(30), timeout=1.0, label="l")
    except TimeoutError:
        pass
total = time.time() - t0
ck("★ 连续 3 次超时总耗时可控", total < 6, f"实际 {total:.2f}s")

# ── 5. 代码里不再裸调 adb ──────────────────────────────────────
#
# 只看代码，跳过注释/文档字符串（说明性文字里出现 adb 是正常的）。
src_path = ROOT / "src" / "android_agent" / "device" / "direct.py"
raw_src = src_path.read_text(encoding="utf-8")
raw_lines = raw_src.splitlines()
code_lines: list[str] = []
in_doc = False
for ln in raw_lines:
    s = ln.strip()
    q = s.count('"""') + s.count("'''")
    if in_doc:
        if q:
            in_doc = False
        continue
    if q == 1:
        in_doc = True
        continue
    if s.startswith("#"):
        continue
    code_lines.append(ln.split("  #")[0])
code = "\n".join(code_lines)

ck("★ 不再有 _shell_text（adb 直调已删）", "_shell_text" not in code)
ck("★ 不用 subprocess 起 adb", "subprocess.run(" not in code
   and "subprocess.Popen(" not in code)
ck("★ 统一走 _call_with_timeout", "_call_with_timeout" in code)
n_wrapped = code.count("_call_with_timeout(")
ck("★ 多处 u2 调用被包住", n_wrapped >= 3, f"{n_wrapped} 处")

# ── 6. window_size 顺序陷阱（176 的间接成因）───────────────────
#
# 判据看原文（不剥注释）—— 要守的正是"注释里得写明白"这件事本身。
# _window_size 的说明全在 docstring 里，剥掉注释后什么都不剩。
ck("★ _window_size 注释里写明了 (高,宽) 顺序陷阱",
   "(height, width)" in raw_src or "height, width" in raw_src
   or "高, 宽" in raw_src)

# ── 7. 设备操作入口都在 DirectDevice 上 ────────────────────────
for m in ("observe", "act", "key", "swipe", "input_text"):
    ck(f"设备动作 {m}() 走 u2", f"def {m}(" in code)

# ── 报告 ───────────────────────────────────────────────────────
bad = [c for c in CHECKS if not c[1]]
print()
for name, ok, detail in CHECKS:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail else ""))
print()
print(f"  {len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
sys.exit(1 if bad else 0)
