"""skill_root 传错路径的诊断质量 —— WinError 267 踩过。

背景（实测）：
    报错是 `NotADirectoryError: [WinError 267] 目录名称无效`，
    出现在很深的 subprocess 层（`cwd=str(self.skill_root)`）。
    **完全看不出是 skill_root 传错了** —— 看起来像 run.py/子进程的问题。

    静态读代码看不出来，因为坏值来自运行时实参。

这个测试盯住：**坏路径必须在入口就被拦住，且报错要直接说清是什么错。**
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.device.bridge import RealDevice  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


def expect_error(skill_root):
    """构造 RealDevice，返回异常文本；没报错返回 None。"""
    try:
        RealDevice(skill_root, serial="x")
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


# 一个不存在的路径 —— 就是 WinError 267 的触发条件
GHOST = str(Path.home() / ".no_such_project" / "assets")

print("=== 1) ★ 不存在的路径要指向确切原因 ===")
err = expect_error(GHOST)
check("抛的是 ValueError（不是等到底层 OS 错误）",
      err is not None and err.startswith("ValueError"), (err or "没报错")[:40])
if err:
    check("明说「不是有效目录」", "不是有效目录" in err)
    check("提示该目录应含什么", "tools/run.py" in err)

print("\n=== 2) 空串要能识别（它不报 267，表现完全不同）===")
# Path("") == Path(".")，所以 cwd 落在当前目录、run.py 找不到 ——
# 现象是「找不到 run.py」，不是路径错误。容易被当成两个不同的问题。
err = expect_error("")
check("空串也抛 ValueError", err is not None and err.startswith("ValueError"),
      (err or "没报错")[:40])
if err:
    check("明说 run.py 不存在", "tools/run.py" in err)

print("\n=== 3) 存在但不对的目录（有目录、没 run.py）===")
err = expect_error(str(HERE))          # langgraph-poc 根目录，有 .venv 但没 tools/
check("也抛 ValueError", err is not None and err.startswith("ValueError"),
      (err or "没报错")[:40])
if err:
    check("提示该目录应含什么", "tools/run.py" in err and "knowledge" in err)

print("\n=== 4) ★ 正确路径必须照常构造（别把正常的也拦了）===")
try:
    d = RealDevice(str(HERE / "assets"), serial="x")
    check("正确路径构造成功", d.run_py.is_file(), str(d.run_py))
    check("skill_root 解析正确", d.skill_root == HERE / "assets")
except Exception as e:  # noqa: BLE001
    check("正确路径构造成功", False, f"{type(e).__name__}: {e}")

print("\n=== 5) 报错必须在**构造时**就出现，不能等到 subprocess ===")
# 这条是本次 bug 的核心：错误暴露得太晚、太深、太不像它自己。
import inspect  # noqa: E402
src = inspect.getsource(RealDevice.__init__)
check("校验写在 __init__ 里", "不是有效目录" in src)
check("校验在 _run 之前（构造即失败）", "def _run" not in src)

print("\n=== 6) 实证：坏 cwd 确实会抛 WinError 267（记录现象）===")
# 把这个 OS 行为钉下来 —— 以后有人看到 267 能立刻联想到 skill_root。
try:
    subprocess.run([sys.executable, "-c", "print(1)"],
                   capture_output=True, timeout=30,
                   cwd=r"D:\no\such\dir\xyzzy")
    check("不存在的目录做 cwd 会抛异常", False, "居然没抛")
except NotADirectoryError as e:
    check("不存在的目录做 cwd -> WinError 267",
          "267" in str(e), str(e)[:60])

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
