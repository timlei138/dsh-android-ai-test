"""冷启动必须自己唤醒屏幕 —— 锁屏会让一切点击静默失效。

实测踩过（168 用例）：
    设备自己休眠了（`mWakefulness=Dozing`，`mCurrentFocus=NotificationShade`），
    结果显示 18 轮、steps 1/4、BLOCKED，模型「连续 3 次点 (0,0)」。

    症状看起来像**模型不会用**（连点同一个无效坐标、不肯换目标），
    其实是**设备根本没醒** —— 屏幕上没有可点控件，模型只能拿到 (0,0)。
    又是一类"骗过了日志"的故障：日志里全是模型的错，没有一句提到屏幕状态。

修复：冷启动第一步先 WAKEUP + dismiss-keyguard，再 force-stop / 拉起 App。
这个测试盯住这段逻辑存在、且在正确的位置（在拉起 App 之前）。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))
sys.stdout.reconfigure(encoding="utf-8")

SRC = (HERE / "assets" / "tools" / "session.py").read_text(encoding="utf-8")

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


print("=== 1) 唤醒/解锁逻辑存在 ===")
check("有 KEYCODE_WAKEUP", "KEYCODE_WAKEUP" in SRC)
check("有 dismiss-keyguard", "wm dismiss-keyguard" in SRC)
check("会查 mWakefulness", "mWakefulness=" in SRC)
check("会查 mCurrentFocus", "mCurrentFocus" in SRC)

print("\n=== 2) ★ 必须在拉起 App 之前（否则屏幕还是黑的）===")
i_wake = SRC.find("KEYCODE_WAKEUP")
i_force = SRC.find('am force-stop {pkg}')
i_monkey = SRC.find("monkey -p {pkg}")
check("force-stop 之前有唤醒", i_wake != -1 and i_force != -1 and i_wake < i_force,
      f"wake@{i_wake} < force-stop@{i_force}")
check("monkey 之前有唤醒", i_wake != -1 and i_monkey != -1 and i_wake < i_monkey,
      f"wake@{i_wake} < monkey@{i_monkey}")

print("\n=== 3) 只在需要时才唤醒（别每次都发一遍）===")
# 无条件发 WAKEUP 会有副作用：用户手动关屏时被强行点亮。
seg = SRC[max(0, i_wake - 400):i_wake + 200]
check("唤醒包在 'Awake' not in 判断里", "Awake" in seg, "条件唤醒")
check("解锁包在 NotificationShade/Keyguard 判断里",
      "NotificationShade" in seg or "Keyguard" in seg)

print("\n=== 4) 失败不能拖垮冷启动（唤醒是尽力而为）===")
# except 在 dismiss-keyguard 之后，窗口要覆盖到那里
i_dismiss = SRC.find("wm dismiss-keyguard")
wake_block = SRC[max(0, i_wake - 500):i_dismiss + 300]
check("唤醒段有 try/except", "except Exception" in wake_block)
check("唤醒失败不被当成冷启动失败",
      "restart_note" not in SRC[i_wake:i_dismiss + 300],
      "唤醒段的异常不该污染 restart_note")

print("\n=== 5) _shell_text 兼容 ShellResponse / str 两种返回 ===")
check("有 _shell_text",
      re.search(r"def _shell_text\(", SRC) is not None)
body = SRC[SRC.find("def _shell_text("):][:600] if "_shell_text" in SRC else ""
check("用 getattr(out, 'output', out) 兼容两种形态",
      'getattr(out, "output", out)' in body)
check("None 也兜住", "if out is None" in body)

print("\n=== 6) 有 --no-restart 时不该唤醒（尊重用户）===")
# 整段都在 `not args.no_restart` 分支里
i_block = SRC.find("if d is not None and pkg and not args.no_restart")
check("唤醒在 not args.no_restart 分支内",
      i_block != -1 and i_block < i_wake, f"block@{i_block} < wake@{i_wake}")

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
