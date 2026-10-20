"""用例层：读 YAML、解析成结构化 spec、算路径。

这一层**不碰设备**。给它一个用例文件名，它告诉你这条用例有哪些步骤、
每步的落点判据是什么、前置条件要做什么。

路径计算集中在这里的 `paths` 模块 —— 别处不要再自己拼
`Path(__file__).parent / "assets"`：文件一挪位置就错，而且是静默错。
"""

from __future__ import annotations

from .paths import (REPO_ROOT, assets_dir, cases_dir, knowledge_dir,  # noqa: F401
                    runner_python, skill_root, where)

__all__ = [
    "REPO_ROOT", "assets_dir", "cases_dir", "knowledge_dir",
    "runner_python", "skill_root", "where",
]
