# -*- coding: utf-8 -*-
"""设备读取必须**走 uiautomator2**，且**有超时**，不许无限重试。

## 背景（两个真实故障，都踩过）

**故障一：卡死 20 分钟**
    py-spy 栈：
        close (adbutils/_adb.py:98)
        shell (adbutils/_device_base.py:262)
        __retry_internal (retry/api.py:35)      ← 无限重试
        app_current (uiautomator2/__init__.py:526)
        observe (android_agent/device/direct.py:268)

    u2 的 `app_current()` / `window_size()` 底层是 adbutils 的 `shell()`，
    挂着 `@retry`。**包一层 try/except 挡不住** —— 异常永远不会抛出来，
    它只是不停地重试：CPU 一直涨、日志一个字没有。

**故障二：为了绕开故障一，改成裸调 adb —— 引入了新问题**
    规矩定死：设备操作一律走 uiautomator2（只有 adb logcat 例外）。
    自己拼 adb 命令意味着两套通道，行为还不一致：
    adb 的 `input text` 不送中文，u2 的 setText 正常。

## 所以现在的正确姿势

    `_app_current` / `_window_size` → 都走 u2（`self._d.*`），外面包 `_call_with_timeout`
    即：**用 u2 的能力，用线程保证它一定会返回。**

⚠️ 一个例外（2026-10 修正）：`_app_current` **不用** `u2.app_current()`，
   改用 `self._d.shell("dumpsys activity activities")` ——
   仍然在 uiautomator2 的通道里（没有自建 adb），只是 u2 那个 API
   在页面过渡期会返回旧页面（176 s5 连点 15 格全判无效的根因）。
   "用 uiautomator2" 不等于"连它的 bug 一起用"。

要守的三条：
  ① 读设备的两个入口都走 u2 通道（不自建 adb），
     且 `_app_current` 要读系统真实栈顶而不是 u2 的过渡期读数
  ② 两者都经过 _call_with_timeout
  ③ 拿不到就降级，不许把异常抛出去卡死整条用例
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

OK = 0
BAD = 0
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    global OK, BAD
    if ok:
        OK += 1
        print(f"  [PASS] {name}" + (f" -> {detail}" if detail else ""))
    else:
        BAD += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


src = (ROOT / "src" / "android_agent" / "device" / "direct.py").read_text(
    encoding="utf-8")

# 剥掉注释和文档字符串，只看真正的代码
code_lines: list[str] = []
in_doc = False
for ln in src.splitlines():
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


def body(fn_name: str) -> str:
    """截出某个方法的代码段（到下一个 def 为止，够用于判据）。"""
    i = code.find(f"def {fn_name}(")
    if i < 0:
        return ""
    j = code.find("\ndef ", i + 1)
    return code[i:j if j > 0 else i + 1500]


print()
print("=" * 70)
print("1) 读设备的两个入口 —— 必须走 uiautomator2")
print("=" * 70)

app = body("_app_current")
ws = body("_window_size")
# ★ 2026-10 修正：`_app_current` **不能**用 u2 的 app_current。
#
# 早先这里的断言是"必须调 self._d.app_current"，方向反了 —— 那是把
# "用 uiautomator2" 误当成"连它的 bug 也一起用"。
# 实际踩到：u2 的 app_current 在页面过渡期返回**旧页面**，
# 176 s5 点空格子要 1.1s 才跳转，读早了就记 changed=False，
# 于是模型以为没反应、连点 15 格全判无效 -> BLOCKED。
#
# 现在：仍然走 u2（`self._d.shell`，没有绕过 uiautomator2），
#       只是把命令换成 dumpsys activity，取系统真实的栈顶。
ck("_app_current 仍走 u2（self._d.shell），不是自建 adb 通道",
   "self._d.shell" in app and "adbutils" not in app)
ck("★ 不再用 u2 的 app_current（它过渡期返回旧页面）",
   "self._d.app_current" not in app)
ck("★ 读的是 dumpsys activity（系统栈顶更准）",
   "dumpsys" in app)
ck("_window_size 调用 u2 的 window_size", "self._d.window_size" in ws)
ck("★ window_size 不用 dumpsys/wm size（那是另一条路，已踩过坑）",
   "dumpsys" not in ws and '"wm"' not in ws and "'wm'" not in ws)

print()
print("=" * 70)
print("2) 两个入口都必须有超时保护")
print("=" * 70)

ck("_app_current 走 _call_with_timeout", "_call_with_timeout" in app)
ck("_window_size 走 _call_with_timeout", "_call_with_timeout" in ws)

import re as _re

to_app = _re.findall(r"timeout=([\d.]+)", app)
to_ws = _re.findall(r"timeout=([\d.]+)", ws)
ck("_app_current 显式给了 timeout", bool(to_app), str(to_app))
ck("_window_size 显式给了 timeout", bool(to_ws), str(to_ws))
ck("超时值合理（1~30s）",
   all(1 <= float(t) <= 30 for t in to_app + to_ws),
   f"app={to_app} ws={to_ws}")

print()
print("=" * 70)
print("3) _call_with_timeout 的实现方式正确")
print("=" * 70)

cw = body("_call_with_timeout")
ck("★ 用线程包住（daemon）", "Thread(" in cw and "daemon=True" in cw)
ck("★ 用 join 带超时", "join(" in cw)
ck("★ 超时后抛 TimeoutError", "TimeoutError" in cw)
ck("★ 超时判定用 is_alive（而不是死等）", "is_alive()" in cw)

print()
print("=" * 70)
print("4) 拿不到就降级，不许抛出去卡死整条用例")
print("=" * 70)

ck("_app_current 有异常兜底", "except Exception" in app)
ck("_window_size 有异常兜底", "except Exception" in ws)
ck("_app_current 拿不到返回空 dict", "return {}" in app)
ck("_window_size 拿不到返回 (0, 0)", "return 0, 0" in ws)

print()
print("=" * 70)
print("5) window_size 的 (高,宽) 顺序陷阱已经写明并可验证")
print("=" * 70)

ck("★ 注释里写明了 (height, width) 顺序",
   "height, width" in src or "高, 宽" in src)
ck("★ 返回值是个二元组（宽,高 由调用方修正）",
   "return (a, b)" in ws or "return a, b" in ws)

print()
print("=" * 70)
print("6) 不许裸调 adb（除了 logcat）")
print("=" * 70)

ck("★ 代码里没有 _shell_text", "_shell_text" not in code)
ck("★ 代码里没有 subprocess.run 起 adb",
   "subprocess.run(" not in code)
ck("★ 代码里没有 subprocess.Popen 起 adb",
   "subprocess.Popen(" not in code)

print()
print("=" * 70)
print(f"  {OK} 通过 / {BAD} 失败")
if FAILS:
    print("  失败项：" + ", ".join(FAILS))
print("=" * 70)
sys.exit(1 if BAD else 0)
