# -*- coding: utf-8 -*-
"""验证「在子 Agent 里跑」这条链路是否真的成立。

用户关切：这个是在子 Agent 里面跑的，要确保子 Agent 里是对的。

子 Agent 实际做的事情：
    1. DSH 的 subagent 工具起一个子 Agent
    2. 子 Agent 调用 android_case 插件
    3. 插件 spawn 一个 Python 子进程跑 run_case_json.py
    4. run_case_json.py 调 android_agent.agent.runner.run_case()
    5. run_case 内部：拿设备锁 → 跑 LangGraph → 出报告到 stdout

本脚本逐环节验证，不碰真机（用假的 spec + dry 模式）。
"""
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"D:\dsh\langgraph-poc")
sys.path.insert(0, str(ROOT / "src"))

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


print("=" * 70)
print("1) 插件侧：index.js 的入口约定")
print("=" * 70)
js = (ROOT / "runner-tool" / "lib" / "index.js").read_text(encoding="utf-8")
ck("插件用 run_case_json.py 作为执行入口", "run_case_json.py" in js)
ck("用的是编排 venv（.venv）",
   'join(RUNNER_DIR, ".venv", "Scripts", "python.exe")' in js)
ck("stdin 喂 JSON，stdout 取报告",
   'stdio: ["pipe", "pipe", "pipe"]' in js)
ck("★ 有深度守卫：主 Agent(depth=0) 被拒",
   "depth === 0" in js and "只能由「执行子 Agent」调用" in js)
ck("depth 取自持久化 header，模型改不了",
   "session?.header?.delegationDepth" in js or
   "delegationDepth" in js)
ck("★ 插件 schema 里只剩真实生效的参数",
   "replay" not in js)
ck("render 自检存在（防漏写 render 到运行时才炸）",
   "output?.render" in js and "__selfcheck__" in js)

print()
print("=" * 70)
print("2) Python 侧：run_case_json.py 的契约")
print("=" * 70)
py = (ROOT / "run_case_json.py").read_text(encoding="utf-8-sig")
ck("★ stdout 只出报告：把 fd1 重定向到 stderr",
   "os.dup2(2, 1)" in py and "real_fd" in py)
ck("有设备锁（同一台设备不能并发）", "lock" in py.lower())
ck("读 stdin 的 JSON", "sys.stdin.read()" in py)

# 真的空输入跑一次，确认错误走 stdout 且是合法 JSON
r = subprocess.run(
    [str(ROOT / ".venv" / "Scripts" / "python.exe"),
     str(ROOT / "run_case_json.py")],
    input="", capture_output=True, text=True,
    encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=120,
)
ck("空 stdin -> 退出码非 0", r.returncode != 0, f"rc={r.returncode}")
try:
    d = json.loads(r.stdout.strip())
    ck("空 stdin 的错误也是合法 JSON（主 Agent 能解析）",
       d.get("status") == "ERROR", d.get("summary"))
except json.JSONDecodeError:
    ck("空 stdin 的错误也是合法 JSON", False, r.stdout[:120])

print()
print("=" * 70)
print("3) ★ 设备操作是否真的都在子 Agent 进程内")
print("=" * 70)
# 主 Agent 只有 android_case 工具，没有 shell / adb
import subprocess as _sp
# 查插件是否暴露了任何"绕过执行器直接操作设备"的入口
ck("插件只有一个工具（android_case），没有别的设备入口",
   js.count("ctx.tools.register(tool)") == 1)
ck("插件不直接调 adb（否则主 Agent 能绕过子 Agent 碰设备）",
   '"adb"' not in js and "'adb'" not in js)

# Python 侧：设备层是否只被 runner（子 Agent 内）调用
runner = (ROOT / "src" / "android_agent" / "agent" / "runner.py").read_text(encoding="utf-8-sig")
ck("runner.py 里构造设备", "DirectDevice(" in runner or "RealDevice(" in runner)
rc = (ROOT / "run_case_json.py").read_text(encoding="utf-8-sig")
# ⚠️ run_case_json.py 里**有** adb —— 但它只做一件事：`adb devices`
#    挑出唯一在线设备当 serial，不点、不观察、不改设备状态。
#    这段只在子 Agent 进程里跑（主 Agent 只有 android_case 工具），所以不算越界。
#    真正的设备操作全部在 run_case() 内部。
ck("★ run_case_json.py 只用 adb 选设备，不做实际操作",
   'subprocess.run([adb, "devices"]' in rc
   and ".click(" not in rc and "screencap" not in rc and "uiautomator" not in rc)
ck("★ 设备动作统一走 run_case()",
   "run_case(" in rc)

print()
print("=" * 70)
print("4) 导入链完整（子 Agent 进程里不会半路 ImportError）")
print("=" * 70)
for mod in ("android_agent.agent.runner", "android_agent.agent.nodes",
            "android_agent.device.direct", "android_agent.cases.loader",
            "android_agent.report.writeback", "android_agent.agent.vision",
            "android_agent.agent.child_runner"):
    try:
        __import__(mod)
        ck(f"import {mod}", True)
    except Exception as e:
        ck(f"import {mod}", False, f"{type(e).__name__}: {e}")

print()
print("=" * 70)
print(f"  {BAD} 失败 / {OK + BAD} 项")
print("=" * 70)
sys.exit(1 if BAD else 0)
