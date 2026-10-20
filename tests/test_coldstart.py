"""冷启动复位的两个真机 bug（2026-10 修）。

用户原话：
    「我旧的说过如果是除非要清理APP。否则都要冷启动。那就不应该有这种问题」
    「这个是个bug。杀死日历后日历吊起的图片选择框还在前台」

两个 bug 都是**真机实测**发现的，而且都会让"起点固定"这个前提静默失效：

bug1：`adbutils.app_stop/app_start` 静默失效
    实测：app_stop 后 pidof 仍返回原 pid；app_start 返回 None 且没拉起。
    **代码却不报错**，还打印"已冷启动复位" —— 最坏的一类 bug：骗过了日志。
    修：改用 `am force-stop` + `monkey`，并**回读前台校验**。

bug2：杀掉 App 后它拉起的系统浮层还留在前台
    实测：日历调起 PhotoPicker（com.android.providers.media.module），
    杀掉日历后这个框还在前台挡着（不属于日历进程，force-stop 日历动不了它）。
    修：冷启动前先 force-stop 这类跨包浮层宿主。

这个测试盯住"下次改回去"和"漏掉新浮层宿主"两种情况。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets" / "tools"))
sys.stdout.reconfigure(encoding="utf-8")

from session import (_OVERLAY_HOSTS, _foreground_package, _is_overlay_of,  # noqa: E402
                     _overlay_packages)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


class FakeDev:
    def __init__(self, out=""):
        self.out = out

    def shell(self, cmd):
        return self.out


class FakeResp:
    """adbutils 原生 shell() 的返回类型：要取 .output。"""

    def __init__(self, out):
        self.output = out


print("=== 0) ★ shell() 两种返回形态都要兼容（实测踩过）===")
# adbutils 原生 device.shell() 返回 ShellResponse；本项目的包装层返回 str。
# 只按一种写会炸 `expected string or bytes-like object, got 'ShellResponse'`，
# 冷启动整个失败 —— 比原来的静默失效还糟（这次是"报了错但功能全丢"）。
_F = "  mCurrentFocus=Window{c092396 u0 com.zui.calendar/com.zui.calendar.AllInOneActivity}"
check("str 形态", _foreground_package(FakeDev(_F)) == "com.zui.calendar", "")
check("ShellResponse 形态（取 .output）",
      _foreground_package(FakeDev(FakeResp(_F))) == "com.zui.calendar", "")
check("None 不炸", _foreground_package(FakeDev(None)) == "", "")
check(".output 为 None 不炸",
      _foreground_package(FakeDev(FakeResp(None))) == "", "")

print("\n=== 1) 回读前台：必须能认出当前 App（这是校验冷启动的唯一手段）===")
FOCUS_CAL = ("  mCurrentFocus=Window{c54ff29 u0 com.zui.calendar/"
             "com.zui.calendar.AllInOneActivity}")
FOCUS_PICKER = ("  mCurrentFocus=Window{529a7ab u0 "
                "com.android.providers.media.module/com.android.providers."
                "media.photopicker.PhotoPickerGetContentActivity}")
FOCUS_LAUNCHER = "  mCurrentFocus=Window{5780384 u0 com.zui.launcher/com.zui.launcher.drawer.NormalLauncher}"

check("认出日历", _foreground_package(FakeDev(FOCUS_CAL)) == "com.zui.calendar",
      _foreground_package(FakeDev(FOCUS_CAL)))
check("认出 PhotoPicker（带 u0 前缀也要拆对）",
      _foreground_package(FakeDev(FOCUS_PICKER))
      == "com.android.providers.media.module",
      _foreground_package(FakeDev(FOCUS_PICKER)))
check("认出桌面", _foreground_package(FakeDev(FOCUS_LAUNCHER))
      == "com.zui.launcher", _foreground_package(FakeDev(FOCUS_LAUNCHER)))
check("null 不当成包名", _foreground_package(FakeDev("  mCurrentFocus=null")) == "",
      repr(_foreground_package(FakeDev("  mCurrentFocus=null"))))
check("空输出不炸", _foreground_package(FakeDev("")) == "")

print("\n=== 2) ★ bug2：跨包浮层要能被识别出来 ===")
# PhotoPicker 仍在时，**算**被测 App 已经在前台吗？
# 算 —— 因为它是被测 App 调起来的，不是"别的 App 抢了前台"。
# 区别很重要：不算的话每次冷启动都会误报失败。
check("PhotoPicker 算日历的浮层",
      _is_overlay_of("com.android.providers.media.module", "com.zui.calendar"))
check("真正的别的 App 不算",
      not _is_overlay_of("com.other.app", "com.zui.calendar"))
check("桌面不算浮层", not _is_overlay_of("com.zui.launcher", "com.zui.calendar"))

print("\n=== 3) ★ bug2：浮层宿主必须涵盖 PhotoPicker ===")
check("含 media.module（PhotoPicker 宿主）",
      "com.android.providers.media.module" in _OVERLAY_HOSTS)
check("含 documentsui（文件选择器）",
      "com.android.documentsui" in _OVERLAY_HOSTS)
check("含 permissioncontroller（系统权限框）",
      "com.android.permissioncontroller" in _OVERLAY_HOSTS)
check("含 systemui",
      "com.android.systemui" in _OVERLAY_HOSTS)

print("\n=== 4) 清理清单不能包含被测包自身（否则刚拉起又被杀）===")
pkgs = _overlay_packages("com.zui.calendar")
check("不含被测包", "com.zui.calendar" not in pkgs, "")
check("数量 = 全部 - 1", len(pkgs) == len(_OVERLAY_HOSTS))
# 被测包**就是**浮层宿主时（比如测 PhotoPicker 本身），不能把它清掉
check("被测包是浮层宿主时也不清它",
      "com.android.documentsui" not in _overlay_packages("com.android.documentsui"),
      "")

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
