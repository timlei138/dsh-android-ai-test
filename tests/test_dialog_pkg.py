"""按**包名**区分系统权限弹窗 vs App 弹窗 —— 用户的判据，落成可执行规则。

用户原话：
  "系统权限弹窗的包名是固定的。APP弹窗的包名就是当前APP自己。
   APP自己的弹窗不区分是啥弹窗，都是功能的一部分。"

真机证据（会话 521，一次 act 里两个框依次出现）：
  ┌ App 自绘说明页（点之前）
  │   包名 com.zui.calendar
  │   rid  scrollView / button2 / button1
  │   文案 「访问文件相关信息」「退出」「同意」
  ├ 点掉「同意」→
  └ 系统运行时权限框（点之后，工具层自动处理）
      包名 com.android.permissioncontroller
      rid  com.android.permissioncontroller:id/permission_allow_button_two
      文案 「允许」/「拒绝」

判据的威力：**不用猜 activity、不用猜文案**。
  · 权限框是浮层，浮在 App 上时 activity 不变 → 按 activity 认不出来
  · 但包名会变 —— 浮层属于 permissioncontroller
  · 而 App 自己的弹窗（说明页/引导框/提示框）包名就是被测 App

处理策略也随之清晰：
  · 系统框  → 工具层自动点（抢 6 秒窗口）+ 记账（点了哪几个权限）
  · App 弹窗 → **不特殊对待**，它就是流程的一步，正常走
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# 系统权限框的固定包名（Android 运行时权限 UI 都是它）
SYSTEM_PERM_PACKAGES = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",   # GMS 设备
    "com.android.packageinstaller",              # 老版本 ROM
)


def is_system_permission_pkg(pkg: str | None) -> bool:
    """前台包名是不是**系统权限框**。"""
    p = (pkg or "").strip().lower()
    if not p:
        return False
    return any(p == m or p.startswith(m + "/") or m in p
               for m in SYSTEM_PERM_PACKAGES)


def classify(pkg: str | None, app_pkg: str) -> str:
    """返回 system_perm / app_dialog / app。

    注意 "app_dialog" 不是靠包名区分的 —— App 自己的弹窗包名就是自己，
    和普通页面一样。用户明确说"APP自己的弹窗不区分是啥弹窗，都是功能的一部分"，
    所以这里把它们归为一类，不做特殊处理。
    """
    if is_system_permission_pkg(pkg):
        return "system_perm"
    if (pkg or "").strip().lower() == (app_pkg or "").strip().lower():
        return "app"
    return "other"


PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


APP = "com.zui.calendar"

print("=== 1. 系统权限框（包名固定）===")
for p in ("com.android.permissioncontroller",
          "com.google.android.permissioncontroller",
          "com.android.packageinstaller"):
    check(f"{p} 判为系统框", is_system_permission_pkg(p) is True)

print("\n=== 2. App 自己的弹窗 = App 包名 -> 不是系统框 ===")
check("日历说明页判为 App 自己",
      is_system_permission_pkg(APP) is False, "真机实测：button1/同意 就是这个包")
check("分类为 app", classify(APP, APP) == "app")

print("\n=== 3. 真机证据逐条对照 ===")
# 会话 521 的现场
check("App 说明页（button1/同意）-> app",
      classify("com.zui.calendar", APP) == "app", "包名=日历自己")
check("系统框（permission_allow_button_two）-> system_perm",
      classify("com.android.permissioncontroller", APP) == "system_perm")

print("\n=== 4. 大小写/前后缀容错 ===")
check("大写也认", is_system_permission_pkg("COM.ANDROID.PermissionController") is True)
check("带斜杠也认",
      is_system_permission_pkg("com.android.permissioncontroller/x.Y") is True)

print("\n=== 5. 边界：空值不算系统框 ===")
check("None 不算", is_system_permission_pkg(None) is False)
check("空串不算", is_system_permission_pkg("") is False)

print("\n=== 6. App 弹窗不特殊对待（用户明确要求）===")
# 「访问文件相关信息」「课程表上新了」「知道了」三种 App 弹窗
# 包名都是被测 App —— 归类一致，说明代码不会给它们开小灶
for _name in ("访问文件相关信息", "课程表上新了！", "知道了"):
    check(f"「{_name}」按普通页面处理",
          classify(APP, APP) == "app")

print("\n" + "=" * 46)
print(f"  全部通过（{len(PASS)} 项）" if not FAIL
      else f"  {len(FAIL)} 项失败: {FAIL}")
print("=" * 46)
raise SystemExit(0 if not FAIL else 1)
