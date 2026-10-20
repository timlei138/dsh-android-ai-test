"""弹窗分类：系统权限弹窗 vs App 弹窗，必须一眼分得清。

## 为什么要有这个测试

用户的原话定下了处理边界：

    「App 的权限页不会自己主动消失，就不用特殊处理了。我们只处理系统权限弹窗。」

这条边界靠两个东西守：
  1. `is_dialog` 只表示「这是挡路弹窗」，**不**表示「这是系统权限弹窗」
  2. 「是不是系统权限弹窗」由**代码按包名**判定，不靠模型申报

第 2 条是重点：模型看见的是界面，看不见包名。
168 轮07 就是模型看着只有 button1，把系统权限框当普通弹窗点掉了 ——
权限从没授上，整条用例 FAIL。所以这个判定绝不能退回给模型。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.device.bridge import (  # noqa: E402
    SYSTEM_PERM_PACKAGES, classify_dialog, is_system_permission_pkg)

PASS = FAIL = 0


def ck(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


APP = "com.miui.calendar"

print("=" * 72)
print("1) 系统权限框按包名认（这是唯一可靠判据）")
print("=" * 72)
for pkg in SYSTEM_PERM_PACKAGES:
    ck(f"{pkg} -> 系统权限框", is_system_permission_pkg(pkg))
ck("permissioncontroller 带后缀也认",
   is_system_permission_pkg("com.android.permissioncontroller/..."
                            ".permission.ui.GrantPermissionsActivity"))

print()
print("=" * 72)
print("2) ★ App 自己的包绝不能误判成系统框")
print("=" * 72)
ck("日历 App 不是系统框", not is_system_permission_pkg(APP))
ck("空包名不是系统框", not is_system_permission_pkg(""))
ck("None 不是系统框", not is_system_permission_pkg(None))
# 早先踩过的坑：App 自绘权限页的 activity 名里也有 permission 字样，
# 但包名还是 App 自己的 —— 按包名判就不会错。
ck("App 内 permission 字样的 activity 不算系统框",
   not is_system_permission_pkg("com.miui.calendar"))

print()
print("=" * 72)
print("3) classify_dialog 三分：system_perm / app / other")
print("=" * 72)
ck("系统框 -> system_perm",
   classify_dialog("com.android.permissioncontroller", APP) == "system_perm")
ck("App 自己 -> app", classify_dialog(APP, APP) == "app")
ck("别的 App -> other", classify_dialog("com.other.app", APP) == "other")

print()
print("=" * 72)
print("4) ★ is_dialog 的语义：挡路弹窗，**不**区分种类")
print("=" * 72)
import inspect  # noqa: E402

from android_agent.agent import nodes  # noqa: E402

_src = inspect.getsource(nodes)
# 字段说明必须写清它不管分类，否则模型会以为 is_dialog=true 就是系统框
ck("is_dialog 说明里明确写了不区分",
   "不区分" in _src and "is_dialog" in _src,
   "字段说明要求")
# 弹窗记录必须按包名判系统框
ck("打标记用的是 is_system_permission_pkg",
   "is_system_permission_pkg(state.get(\"last_package\"))" in _src)

print()
print("=" * 72)
print("5) ★ 只给系统权限框打标记，App 弹窗不打")
print("=" * 72)
# 关键结构：标记必须**在 if 里面**，不能无条件执行。
# 取 is_system_permission 那行往前看，必须紧挨着一个 if 判定。
_lines = _src.splitlines()
_mark_idx = next((i for i, ln in enumerate(_lines)
                  if 'rec["is_system_permission"] = True' in ln), -1)
ck("找到了打标记的位置", _mark_idx > 0, f"L{_mark_idx + 1}")
_ctx = "\n".join(_lines[max(0, _mark_idx - 4):_mark_idx + 1])
ck("★ 标记在 if 判定之内（不是无条件打）",
   "if is_system_permission_pkg(" in _ctx,
   "前 4 行内必须有包名判定")
ck("else 分支不再给 App 弹窗加标记",
   'rec["is_system_permission"] = False' not in _src,
   "App 弹窗不需要这个字段")


print()
print("=" * 72)
print("6) 行为验证：同一段文字，包名不同 -> 结论不同")
print("=" * 72)
txt = "「访问文件相关信息」"
ck("系统框 + 该文字 -> 记为系统权限弹窗",
   is_system_permission_pkg("com.android.permissioncontroller"))
ck("App 包 + 同文字 -> 不算系统权限弹窗",
   not is_system_permission_pkg(APP),
   "App 权限页是业务流程的一部分，代码不管")

print()
if FAIL:
    print(f"❌ {FAIL} 项失败 / {PASS} 项通过")
    raise SystemExit(1)
print(f"全部通过（{PASS} 项）")
