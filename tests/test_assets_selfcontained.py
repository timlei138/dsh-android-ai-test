"""资产与运行环境的自包含性。

这个测试盯住四件事：
  1. 资产目录在仓库内，能独立于任何外部目录解析
  2. 只有一个运行环境（仓库 .venv），且**真的能用**
  3. paths.runner_python() 解析到的就是它
  4. 没有代码写死外部绝对路径（写死了会静默读到空目录）
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.cases.paths import (assets_dir, cases_dir, graph_python,  # noqa: E402
                                       runner_python)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


print("=== 1) 资产目录必须在仓库内，不依赖外部目录 ===")
check("assets/ 就是工作区内的目录",
      assets_dir() == HERE / "assets", str(assets_dir()))
# ⚠️ 这条原来断言"用例目录非空"。写死路径会读成空目录，用它当哨兵是对的 ——
# 但用例会被**主动清理**，清空是合法状态，不是故障。
# 所以哨兵改成"目录存在且可读"，再单独确认一份**一定该在**的资产还在 ——
# 那才是"路径写死 -> 读到别的空目录"真正会暴露的症状。
# 原来拿 SPEC.md 当哨兵，那份 YAML 规范已随 YAML 一起删除；
# 现在改用知识卡目录的模板，它同在 assets/ 下且长期存在。
check("用例目录存在且可读",
      cases_dir().is_dir(), str(cases_dir()))
_tpl = assets_dir() / "knowledge" / "_template.md"
check("资产目录的常驻文件在（knowledge/_template.md）",
      _tpl.is_file(), str(_tpl))

print("\n=== 2) ★ 只有一个运行环境（仓库根 .venv）===")
# 历史：这里曾经要求 assets/.venv 存在，因为 OCR 库 rapidocr-onnxruntime
# 停更在 1.2.3、不支持 Python 3.13，设备那套只能憋在 3.12 的独立 venv 里。
# 后来改用新包名 rapidocr（3.10.x，支持 3.13），两个环境已合并成一个。
# 现在要守的是反面：**不该再有** assets/.venv —— 那说明又分家了。
dev_py = HERE / ".venv" / "Scripts" / "python.exe"
check("仓库根 .venv 存在", dev_py.is_file(), str(dev_py))
check("assets/ 下没有第二个 venv（已合并）",
      not (assets_dir() / ".venv").exists(), str(assets_dir() / ".venv"))
check("requirements.txt 存在（能重建）",
      (HERE / "requirements.txt").is_file())
check("setup_env.py 存在（第一跑自动装）",
      (HERE / "setup_env.py").is_file())

print("\n=== 3) ★ runner_python 必须解析到那个唯一环境 ===")
rp = runner_python()
check("解析到仓库根 .venv", Path(rp) == dev_py, rp)
# 编排和工具用同一个解释器 —— 这正是合并的意义：
# 以前两边版本不同会行为漂移，报错时还得先想"这是哪个环境的问题"。
check("和 graph_python 是同一个环境",
      Path(runner_python()) == Path(graph_python()), rp)

print("\n=== 4) ★ 这个环境真的能用（编排 + 设备 + OCR 依赖都在）===")
if dev_py.is_file():
    r = subprocess.run(
        [str(dev_py), "-c",
         "import langgraph, adbutils, cv2, rapidocr, PIL, numpy, yaml; print('ok')"],
        capture_output=True, text=True, timeout=180)
    out = (r.stdout or r.stderr).strip().splitlines()
    tail = out[-1] if out else "(空)"
    check("langgraph/adbutils/cv2/rapidocr/PIL/numpy/yaml 全部可导入",
          r.returncode == 0, tail[:100])

print("\n=== 5) run.py 在设备 venv 下能起来 ===")
if dev_py.is_file():
    run_py = assets_dir() / "tools" / "run.py"
    check("tools/run.py 存在", run_py.is_file())
    if run_py.is_file():
        r = subprocess.run([str(dev_py), str(run_py), "--help"],
                           capture_output=True, text=True, timeout=180)
        combined = (r.stdout or "") + (r.stderr or "")
        # 见到"缺依赖"就说明解释器选错了 —— 这正是当时的故障现象
        check("不报「缺依赖」", "缺依赖" not in combined,
              "缺依赖" if "缺依赖" in combined else "ok")

print("\n=== 6) 没有代码写死外部绝对路径 ===")
# 写死路径的坏处：外部目录变了不会报错，只会静默读到空目录，
# 或者更糟 —— 一半读新一半读旧。所以一律走 paths.skill_root()。
#
# ⚠️ 只看**可执行代码**，不看注释/文档字符串 ——
# 文档里写"原本指向 ~/.agents/..."是正确的说明，不该被当成违规。
# 误报会让人把测试关掉，比不测更糟。
HARDCODED = re.compile(r"D:[\\/]dsh[\\/]|\.agents[\\/]skills")


def code_lines(text: str):
    """产出 (行号, 行内容)，跳过注释与文档字符串。"""
    in_doc = False
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # 用三引号配对判断是否在文档字符串里（够用了：本项目没有奇怪的嵌套）
        if stripped.count('"""') % 2 == 1:
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        yield i, line


offenders = []
for py in sorted(HERE.glob("*.py")):
    if py.name == Path(__file__).name:
        continue                      # 本文件自身要提这个字符串
    text = py.read_text(encoding="utf-8", errors="replace")
    for i, line in code_lines(text):
        if HARDCODED.search(line):
            offenders.append(f"{py.name}:{i}")
check("可执行代码里无硬编码外部路径", not offenders,
      " / ".join(offenders[:4]) if offenders else "0 处")

# ★ 反面：paths.py 里**不该再有** LEGACY 常量。
#   它曾经是"退到旧技能目录"的兜底，那等于静默降级：
#   不报错，但用错规则继续跑 —— 比直接报错危险得多。已彻底删掉，
#   这条断言守着它不再回来。
_ap = (HERE / "src" / "android_agent" / "cases" / "paths.py").read_text(encoding="utf-8")
check("paths.py 里没有 LEGACY 兜底常量", "LEGACY" not in _ap)

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
