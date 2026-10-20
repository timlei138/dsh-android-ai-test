"""验证「前提跑完不在 App 里就自动拉起」这条兜底。

真机事故（会话538）：
    前提只有 pm clear com.zui.calendar（作者漏写 am start）
    → App 被杀，设备停在 .drawer.NormalLauncher（桌面）
    → 模型在**桌面**上找被测页面，去点桌面搜索框 search_bar_bg
    → 连点 (1432,220) 3 次，43s 后被死循环 blocker 终止

守卫本身是对的（该停就停），但这个失败**本可避免**：
清完数据后把 App 拉起来是件确定性的事，不该指望用例作者记得写。

这个测试确认兜底生效：不在 App 里 → 自动拉起；已经在 → 不乱动。
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

import android_agent.agent.nodes as CA
from android_agent.agent.nodes import prep_node

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
    """记录执行过的 shell 命令，observe 返回可配置的当前包名。"""

    def __init__(self, cur_pkg):
        self.cur_pkg = cur_pkg
        self.shells = []

    def shell(self, cmd):
        self.shells.append(cmd)
        return ""

    def observe_raw(self):
        return {"package": self.cur_pkg, "activity": ".X", "next": []}


class Ctx:
    def __init__(self, dev):
        self.device = dev


def run(pkg_name, case_package, preconds):
    dev = FakeDev(pkg_name)
    state = {"task": {"package": case_package, "preconditions": preconds}}
    with patch.object(CA, "_ctx", lambda: Ctx(dev)), \
         patch.object(CA.time, "sleep", lambda *_: None):
        out = prep_node(state)
    return out, dev


print("=== 用例 1：pm clear 后停在桌面 → 应自动拉起 ===")
out, dev = run("com.android.launcher", "com.zui.calendar",
               [{"kind": "data", "do": ["pm clear com.zui.calendar"]}])
check("执行了 pm clear", any("pm clear" in c for c in dev.shells), str(dev.shells))
check("自动拉起了被测 App",
      any("monkey -p com.zui.calendar" in c for c in dev.shells), str(dev.shells))
check("记进了 preconditions_done",
      any("auto" in d for d in out.get("pc_done", [])), str(out.get("pc_done")))

print("\n=== 用例 2：已经在被测 App 里 → 不要多此一举 ===")
out, dev = run("com.zui.calendar", "com.zui.calendar",
               [{"kind": "data", "do": ["pm clear com.zui.calendar"]}])
check("没有重复拉起",
      not any("monkey" in c for c in dev.shells), str(dev.shells))

print("\n=== 用例 3：作者自己写了 am start → 拉起后已在 App 里，不重复 ===")
out, dev = run("com.zui.calendar", "com.zui.calendar",
               [{"kind": "data",
                 "do": ["pm clear com.zui.calendar",
                        "am start -n com.zui.calendar/.AllInOneActivity"]}])
check("am start 被执行", any("am start" in c for c in dev.shells), str(dev.shells))
check("没有额外 monkey",
      not any("monkey" in c for c in dev.shells), str(dev.shells))

print("\n=== 用例 4：没写 package → 不猜，跳过兜底 ===")
out, dev = run("com.android.launcher", "",
               [{"kind": "data", "do": ["pm clear com.zui.calendar"]}])
check("没有乱拉起", not any("monkey" in c for c in dev.shells), str(dev.shells))

print(f"\n{'=' * 46}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 46}")
sys.exit(1 if fail else 0)
