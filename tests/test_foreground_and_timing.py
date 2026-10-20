# -*- coding: utf-8 -*-
"""前台读取 + 操作后等待 —— 两个真机踩出来的坑。

## 坑一：`u2.app_current()` 在页面过渡期返回旧页面

176 s5：点周视图空格子，界面 **1.1 秒后**才跳到 EditCourseActivity。
点击后立刻读 app_current 拿到的是 TimetableActivity，于是
记账 changed=False、模型以为没反应、换格子再点 —— 连点 15 格全判无效。
同一个坐标手动点**能进**，说明不是控件点不动，是**读得太早**。

对策（参考 D:\\Project\\python\\AiAgentTest 的做法）：
  不用 u2 的 app_current，改读 `dumpsys activity activities` 的
  `topResumedActivity` —— 那是系统解析栈里真实的栈顶。

## 坑二：操作后要等 1 秒再截图/dump

用户原话：「每个操作等 1s 再截图和 dump xml 就比较稳定了」。

这两条都必须钉住：任何一条退回去，表现都是"点了没反应"，
而那种现象极难从日志上看出来（日志只能说 changed=False，
看不出是界面真的没变，还是我们看早了）。
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")


_src = io.open(str(ROOT / "src" / "android_agent" / "device" / "direct.py"),
               encoding="utf-8").read()

print("=" * 66)
print("坑一：前台读取不走 u2.app_current，走 dumpsys")
print("=" * 66)

# _app_current 的函数体
_m = re.search(r"def _app_current\(self\).*?(?=\n    def )", _src, re.S)
_body = _m.group(0) if _m else ""
ck("_app_current 存在", bool(_body))
ck("不再直接调 self._d.app_current",
   "self._d.app_current" not in _body,
   "还在用 u2 的 app_current —— 页面过渡期会返回旧页面")
ck("改读 dumpsys activity activities",
   "dumpsys activity activities" in _body)

print()
print("--- _parse_top_activity ---")
from android_agent.device.direct import _parse_top_activity  # noqa: E402

# 真实格式（设备实测，activity 以 `.` 开头省略包名前缀）
S1 = ("    topResumedActivity=ActivityRecord{205796433 u0 com.zui.calendar"
      "/.timetable.display.TimetableActivity t455, isEmbedded= false}\n"
      "    * Hist  #1\n")
r = _parse_top_activity(S1)
ck("能解析 topResumedActivity（真实格式）",
   r.get("package") == "com.zui.calendar"
   and r.get("activity") == "com.zui.calendar.timetable.display.TimetableActivity",
   str(r))

S1b = ("  topResumedActivity=ActivityRecord{abc u0 com.zui.calendar/"
       ".timetable.course.EditCourseActivity t789}\n")
r = _parse_top_activity(S1b)
ck("能解析 EditCourseActivity 样式",
   r.get("activity") == "com.zui.calendar.timetable.course.EditCourseActivity",
   str(r))

# 老 ROM：只有 mResumedActivity
S2 = ("  mResumedActivity: ActivityRecord{def u0 com.foo.bar/.MainActivity t1}\n")
r = _parse_top_activity(S2)
ck("回退解析 mResumedActivity",
   r.get("activity") == "com.foo.bar.MainActivity", str(r))

# 全名 activity（不带点前缀）不能被拼两次包名
S3 = ("  topResumedActivity=ActivityRecord{x u0 com.foo.bar/"
      "com.foo.bar.SubAct t1} ")
r = _parse_top_activity(S3)
ck("全名 activity 不重复拼包名",
   r.get("activity") == "com.foo.bar.SubAct", str(r))

ck("空输入返回 {}", _parse_top_activity("") == {})
ck("认不出的输入返回 {}", _parse_top_activity("乱七八糟") == {})

print()
print("=" * 66)
print("坑二：操作后等 1 秒再 dump")
print("=" * 66)

# act 点击后到 observe 之间必须有 sleep(1.0)
_act = re.search(r"def act\(self.*?(?=\n    def )", _src, re.S)
_act_body = _act.group(0) if _act else ""
ck("act 存在", bool(_act_body))
ck("act 点击后 sleep 1 秒",
   re.search(r"t_obs = time\.time\(\)[\s\S]{0,1200}?time\.sleep\(1\.0\)"
             r"[\s\S]{0,200}?scr = self\.observe\(\)", _act_body) is not None,
   "act 里没找到 click -> sleep(1.0) -> observe 的顺序")

_inp = re.search(r"def input_text\(self.*?(?=\n    def )", _src, re.S)
_inp_body = _inp.group(0) if _inp else ""
ck("input_text 输入后也 sleep 1 秒",
   re.search(r"set_text[\s\S]{0,600}?time\.sleep\(1\.0\)", _inp_body)
   is not None)

print()
print("=" * 66)
print("回归：这些做法不许回退")
print("=" * 66)
ck("仍然只用 uiautomator2 做设备操作（不用 adb input/tap）",
   "input tap" not in _src and 'shell("input' not in _src)
ck("u2 调用仍然包了超时（_call_with_timeout）",
   "_call_with_timeout" in _src)
# dumpsys 只应出现在"读前台"这一处**代码**里；其余是注释/说明。
# 用"实际调用"判断，不数注释里提到几次。
_n_dumpsys = len(re.findall(r'shell\(\s*"dumpsys', _src))
ck("代码里真正执行 dumpsys 的只有一处",
   _n_dumpsys == 1, f'实际 shell("dumpsys ... 调用 {_n_dumpsys} 处')

print()
print("=" * 66)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
