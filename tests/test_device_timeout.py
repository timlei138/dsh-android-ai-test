# -*- coding: utf-8 -*-
"""设备读取必须**有超时**，不许无限重试。

为什么单独有这条测试：实测踩到过一次「用例卡死 20 分钟」。

    py-spy 抓到的栈：
        close (adbutils/_adb.py:98)
        shell (adbutils/_device_base.py:262)
        app_current (adbutils/shell.py:457)
        __retry_internal (retry/api.py:35)      ← 无限重试
        app_current (uiautomator2/__init__.py:526)
        observe (android_agent/device/direct.py:268)

    uiautomator2 的 `app_current()` / `window_size()` 底层都是 adbutils 的
    `shell()`，后者挂着 `@retry`（无限重试）。**包一层 try/except 挡不住** ——
    异常永远不会抛出来，它只是不停地重试，进程 CPU 一直涨、日志一个字没有。

所以要守的是：对这两个"会无限重试"的接口，**不许裸调**，
必须走我们自己的、带硬超时的 adb 命令。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

OK = 0
BAD = 0
SRC = (ROOT / "src" / "android_agent" / "device" / "direct.py").read_text(
    encoding="utf-8-sig")


def ck(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        BAD += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


def func_body(name: str) -> str:
    i = SRC.find(f"def {name}(")
    if i < 0:
        return ""
    j = SRC.find("\n    def ", i + 1)
    k = SRC.find("\ndef ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return SRC[i:min(ends) if ends else len(SRC)]


print("=" * 70)
print("1) ★ 不许裸调会无限重试的接口")
print("=" * 70)
for api in ("app_current", "window_size"):
    raw = f"self._d.{api}()"
    ck(f"代码里没有裸调 {raw}", raw not in SRC,
       [ln.strip()[:60] for ln in SRC.splitlines() if raw in ln])

print()
print("=" * 70)
print("2) ★ 改用带超时的包装")
print("=" * 70)
ck("有 _app_current 包装", "def _app_current(" in SRC)
ck("有 _window_size 包装", "def _window_size(" in SRC)
ck("有带超时的 _shell_text", "def _shell_text(" in SRC)

shell = func_body("_shell_text")
ck("_shell_text 传了 timeout", "timeout=timeout" in shell)
ck("_shell_text 直接调 adb（不经过 u2/adbutils）",
   "subprocess.run" in shell and "_adb_exe()" in shell)

print()
print("=" * 70)
print("3) ★ 超时值必须显式，不能是 None")
print("=" * 70)
for fn, _ in (("_app_current", None), ("_window_size", None)):
    body = func_body(fn)
    calls = re.findall(r"timeout=([\w.]+)", body)
    ck(f"{fn} 的调用都显式给了 timeout", bool(calls), calls)
    ck(f"{fn} 没有 timeout=None", "timeout=None" not in body)

print()
print("=" * 70)
print("4) ★ 拿不到就降级，不许抛出去卡死整条用例")
print("=" * 70)
for fn in ("_app_current", "_window_size"):
    body = func_body(fn)
    ck(f"{fn} 有异常兜底", "except Exception" in body)
ck("_app_current 拿不到返回空 dict", "return {}" in func_body("_app_current"))
ck("_window_size 拿不到返回 (0, 0)",
   "return 0, 0" in func_body("_window_size"))

print()
print("=" * 70)
print("5) 多路径兜底：不认单一 dumpsys 字段")
print("=" * 70)
ac = func_body("_app_current")
# 不同 ROM 给的字段不一样，实测这台只有 topResumedActivity
ck("同时看 activity 与 window 两路",
   "dumpsys" in ac and ac.count("dumpsys") >= 2, ac.count("dumpsys"))
ck("认 topResumedActivity", "topResumedActivity" in ac)
ck("也认 mCurrentFocus", "mCurrentFocus" in ac)

print()
print("=" * 70)
print("6) adb 路径可配置，不写死盘符")
print("=" * 70)
ck("有 _adb_exe 辅助", "def _adb_exe(" in SRC)
ck("读环境变量", "ANDROID_ADB" in SRC)
ck("走 PATH 查找", "shutil.which" in SRC)

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
