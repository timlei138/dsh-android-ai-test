# -*- coding: utf-8 -*-
"""子 Agent 执行链路：确认「在子 Agent 里跑」这件事本身是对的。

用户关切原话：
    「这个我是要跑再子Agent里面的。要确保子Agent里面是对的」

所以这里不测业务逻辑（那些在别的测试里），
只测**跑在子 Agent 里的那些前提**：

  1. 主 Agent 调不动 —— 有代码闸门，不是提示词约束
  2. 子 Agent 拿到的报告是干净 JSON，主 Agent 能解析
  3. 设备操作全在执行器进程内，插件不直连 adb
  4. 子 Agent 的 preset 与实际代码一致（不许教它用已删除的参数）
  5. 导入链完整，不会跑到一半 ImportError
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"D:\dsh\langgraph-poc")
PRESET = Path.home() / ".dsh" / ".agent-presets" / "android-runner" / "agent.cordis.yml"

OK = 0
BAD = 0


def ck(name, cond, extra=""):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [PASS] {name}" + (f" -> {extra}" if extra else ""))
    else:
        BAD += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))


JS = (ROOT / "runner-tool" / "lib" / "index.js").read_text(encoding="utf-8")

print("=" * 70)
print("1) ★ 主 Agent 调不动 —— 闸门在代码里，不是提示词")
print("=" * 70)
ck("有 depth 判断", "const depth =" in JS)
ck("主 Agent（depth 0）被明确拒绝",
   "depth === 0" in JS and "只能由「执行子 Agent」调用" in JS)
ck("depth 取自持久化的 header（模型改不了）",
   "session?.header?.delegationDepth" in JS)
ck("拒绝时告诉它改用 subagent（不是干瞪眼）",
   "subagent 工具派发" in JS or "用 subagent" in JS)

print()
print("=" * 70)
print("2) ★ 子 Agent 拿到的报告是干净 JSON")
print("=" * 70)
RC = (ROOT / "run_case_json.py").read_text(encoding="utf-8-sig")
ck("stdout 被保护：fd1 重定向到 stderr",
   "os.dup2(2, 1)" in RC and "real_fd" in RC)
ck("报告用保存的 fd 写出（绕开被改的 fd1）",
   "os.write(real_fd" in RC)
ck("★ 进度日志全走 stderr（不会冲散报告）",
   "os.dup2(2, 1)" in RC)

print()
print("=" * 70)
print("3) ★ 设备操作全在执行器进程内")
print("=" * 70)
ck("插件不直连 adb（否则主 Agent 能绕过子 Agent 碰设备）",
   '"adb"' not in JS and "'adb'" not in JS)
ck("插件只有一个工具入口", JS.count("ctx.tools.register(tool)") == 1)
# 规矩：设备操作一律走 uiautomator2，只有 adb logcat 例外。
# 选设备也不例外 —— 枚举交给 adbutils（只读列表），
# "能不能用"用 u2.connect + window_size 真探一次。
ck("★ run_case_json 不用 adb 命令选设备",
   "subprocess.run([adb" not in RC
   and "subprocess.Popen([adb" not in RC
   and "subprocess.run(['adb" not in RC
   and "subprocess.Popen(['adb" not in RC)
ck("★ 选设备用 u2 真探活", "u2.connect(" in RC and "window_size()" in RC)
ck("★ 选设备不点不截屏不改状态",
   ".click(" not in RC and "screencap" not in RC and "shell(" not in RC)
ck("设备动作统一交给 run_case()", "run_case(" in RC)
ck("整个执行期持有设备锁",
   "DeviceLock" in RC and "_lock.acquire()" in RC and "_lock.release()" in RC)

print()
print("=" * 70)
print("4) ★★ 子 Agent 的 preset 与实际代码一致")
print("=" * 70)
if not PRESET.exists():
    ck("preset 文件存在", False, str(PRESET))
else:
    y = PRESET.read_text(encoding="utf-8")
    ck("preset 存在", True, PRESET.name)
    # preset 在仓库外，改代码时没人会想起它 —— 所以这里盯得紧一点：
    # 已废弃的机制一个字都不许留（详见 test_skill_card_consistency.py）
    ck("★★ preset 不含已废弃的机制",
       not any(w in y for w in ("replay", "replay_stats", "locate_seq")),
       [w for w in ("replay", "replay_stats", "locate_seq") if w in y])

    calls = re.findall(r"^\s*android_case\(([^)]*)\)", y, re.M)
    ck("preset 有正确的调用示例", bool(calls), str(calls))

    ck("preset 有子 Agent 专属 persona（否则它以为自己是主 Agent）",
       "你是**执行子 Agent**" in y)
    ck("persona 明确否掉「再派发」",
       "不是派发" in y and "没有再向下派" in y.replace("不能再向下派", "没有再向下派"))
    ck("toolFilter 是白名单（allow）", "toolFilter:" in y and "allow:" in y)
    ck("★ 白名单里没有 subagent（不许再派孙 Agent）",
       not re.search(r"allow:\s*\n(?:\s*-\s*\w+.*\n)*?\s*-\s*subagent\b", y))
    ck("maxDepth: 1（允许子 Agent、拒绝孙 Agent）",
       "maxDepth: 1" in y)
    ck("白名单里只有 android_case + 只读工具",
       set(re.findall(r"^\s+-\s+(\w+)", y, re.M)) >= {"android_case", "read", "glob", "grep"})

print()
print("=" * 70)
print("5) 导入链完整（子 Agent 进程里不会半路 ImportError）")
print("=" * 70)
for mod in ("android_agent.agent.runner", "android_agent.agent.nodes",
            "android_agent.agent.vision", "android_agent.agent.child_runner",
            "android_agent.device.direct", "android_agent.device.lock",
            "android_agent.cases.loader", "android_agent.report.writeback",
            "android_agent.report.generate"):
    try:
        __import__(mod)
        ck(f"import {mod.split('.')[-1]}", True)
    except Exception as e:
        ck(f"import {mod}", False, f"{type(e).__name__}: {e}")

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
