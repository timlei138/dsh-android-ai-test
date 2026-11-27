# -*- coding: utf-8 -*-
"""跑工具层的全部单测。

用法（toolkit 目录下）：

    .\\.venv\\Scripts\\python.exe tests\\run_all.py

## 为什么现在才有

`tools/` 之前一个单测都没有，改一处只能靠"找个真机跑一遍"来确认 ——
而真机跑一遍要几分钟，于是实际上没人确认。这个目录只放**不需要设备、
不需要数据库、几百毫秒跑完**的纯函数测试：参数解析、判定逻辑、路径归一。
凡是要碰 adb / 真库的，仍然走端到端用例，不在这里。

## 为什么不用 pytest

和 `plugins/android-auto-test-preset/tests/` 一致：纯脚本 + 自己打印
PASS/FAIL + 退出码。少一个依赖，CI 里也不用装。
"""
import io
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

MODULES = [
    "test_capture_meta.py",
    "test_case_expect_split.py",
    "test_case_key.py",
    "test_checklist.py",
    "test_checkpoint.py",
    "test_db_rows.py",
    "test_export_steps.py",
    "test_knowledge_hint.py",
    "test_knowledge_keys_db.py",
    "test_multi_stage_swipe.py",
    "test_perm_resolve.py",
    "test_perm_two_phase.py",
    "test_read_file_shape.py",
    "test_run_dir.py",
    "test_run_pointer.py",
    "test_suspect_timeliness.py",
    "test_system_shade.py",
]


def main() -> int:
    print("=" * 70)
    print(f"跑 {len(MODULES)} 个工具层单测")
    print("=" * 70)
    failed = []
    for name in MODULES:
        path = HERE / name
        print(f"\n--- {name}")
        result = subprocess.run([sys.executable, str(path)], cwd=str(ROOT))
        if result.returncode != 0:
            failed.append(name)

    print("\n" + "=" * 70)
    if failed:
        print(f"  ❌ 失败 {len(failed)} 个：{', '.join(failed)}")
        return 1
    print(f"  ✅ 全部通过（{len(MODULES)} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
