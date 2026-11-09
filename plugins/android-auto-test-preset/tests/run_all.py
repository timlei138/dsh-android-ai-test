# -*- coding: utf-8 -*-
"""跑本工作区的全部测试。

用法（在 D:\\dsh\\android-auto-test 下）：

    .\\run_tests.ps1

或者直接用 Python：

    python tests\\run_all.py

## 为什么不用 pytest

只有 2 个测试文件，都是"纯脚本 + 自己打印 PASS/FAIL + 退出码"的形式。
引入 pytest 要多一个依赖，收益为零。

## 这里测什么、不测什么

测：
  · 我们写的 skill 卡（android-case-check）内容是否正确
  · skill 里不许出现被测 App 的专有名词

不测：
  · 工具层（D:\\Project\\deepseek\\android-ai-test-skills）——
    它自己有 71 个测试，那些测试跟本工作区无关，不在这里跑。
  · preset 的 YAML —— 用 `verify_preset.py` 单独验证（它要读 DSH 的插件目录）。
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

tests = sorted(HERE.glob("test_*.py"))
if not tests:
    print("没有测试文件")
    sys.exit(1)

print("=" * 70)
print(f"跑 {len(tests)} 个测试")
print("=" * 70)

failed: list[str] = []
for t in tests:
    print()
    print(f"--- {t.name}")
    r = subprocess.run([PY, str(t)], cwd=str(ROOT))
    if r.returncode != 0:
        failed.append(t.name)

print()
print("=" * 70)
if failed:
    print(f"  {len(tests) - len(failed)} 通过 / {len(failed)} 失败")
    for f in failed:
        print(f"   ✗ {f}")
    sys.exit(1)
print(f"  ✅ 全部通过（{len(tests)} 个）")
print("=" * 70)
