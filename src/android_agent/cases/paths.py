"""资产目录的唯一解析点。

## 为什么要有这个文件

所有代码都问 `skill_root()`，不自己拼路径。散落的硬编码没法整体迁移 ——
漏一处就会"一半读新、一半读旧"，而且**不会报错**，只会静默读到过期的
知识卡或用例。

## 解析顺序

    1. 环境变量 ANDROID_SKILL_ROOT   —— 部署 / 测试时覆盖
    2. 仓库内的 assets/              —— 正主

**没有第三条兜底**。找不到就抛异常，绝不猜。

## 目录约定

四个子路径缺一不可：

    tools/run.py        设备操作入口（看/动/读）
    knowledge/          知识卡
    storage/vision.json 决策模型凭据
    storage/cases/      用例 YAML
"""
from __future__ import annotations

import os
from pathlib import Path

# 仓库根 = 本文件(src/android_agent/cases/paths.py)往上数三层：
#   cases -> android_agent -> src -> <repo>
# ⚠️ 退的层数必须跟着文件位置走：这个文件一旦挪位置，层数没跟着改，
#    assets/ 就会指到不存在的路径，然后 skill_root 报"找不到资产"。
REPO_ROOT = Path(__file__).resolve().parents[3]

# 资产目录 —— 唯一正主
WORKSPACE_ASSETS = REPO_ROOT / "assets"

# 运行环境 —— 编排与设备操作共用同一个解释器。
# 两个平台都列上：Windows 是 Scripts/python.exe，POSIX 是 bin/python。
_VENV_PYTHONS = (REPO_ROOT / ".venv" / "Scripts" / "python.exe",
                 REPO_ROOT / ".venv" / "bin" / "python")

# 判定"这里是不是一个能用的资产目录"的最小条件
MARKERS = ("tools/run.py", "knowledge", "storage/vision.json")


def _usable(p: Path) -> bool:
    return p.is_dir() and all((p / m).exists() for m in MARKERS)


def skill_root(*, required: bool = True) -> str:
    """返回资产目录。找不到就报错 —— 不要静默退回半残状态。"""
    env = os.environ.get("ANDROID_SKILL_ROOT")
    if env and _usable(Path(env)):
        return str(Path(env).resolve())

    if _usable(WORKSPACE_ASSETS):
        return str(WORKSPACE_ASSETS.resolve())

    if required:
        detail = "\n".join(
            f"    - {m}  " + ("存在" if (WORKSPACE_ASSETS / m).exists() else "不存在")
            for m in MARKERS)
        raise RuntimeError(
            f"资产目录不可用：{WORKSPACE_ASSETS}\n"
            "  需要包含：" + " / ".join(MARKERS) + "\n"
            + detail + "\n"
            "  也可以用 ANDROID_SKILL_ROOT 环境变量指定。")
    return ""


def runner_python() -> str:
    """跑 `tools/run.py` 用的解释器。

    顺序：ANDROID_RUNNER_PYTHON > 仓库 .venv > 当前解释器（并告警）。

    兜底用当前解释器而不是直接抛：有些纯计算路径不碰设备，
    抛了反而误伤。但必须把警告打出来 —— 静默降级会让
    「缺依赖」这个真问题看起来像别的问题。
    """
    env = os.environ.get("ANDROID_RUNNER_PYTHON")
    if env and Path(env).is_file():
        return env
    for c in _VENV_PYTHONS:
        if c.is_file():
            return str(c)

    import sys as _sys
    print(f"[paths] ⚠️ 找不到 .venv，退回当前解释器（{_sys.executable}）。"
          "如果它不是项目环境，设备操作会报「缺依赖」。"
          "跑一次 `python setup_env.py` 可以自动装好。",
          file=_sys.stderr)
    return _sys.executable


def graph_python() -> str:
    """跑 LangGraph 编排的解释器。

    和 runner_python 是同一个环境。分开两个名字是因为调用点语义不同
    （"跑编排" vs "跑工具"），将来若真要拆开，只改这里。
    """
    return runner_python()


def assets_dir() -> Path:
    """资产目录的 Path 形式。"""
    return Path(skill_root())


def cases_dir() -> Path:
    """用例 YAML 目录。"""
    return assets_dir() / "storage" / "cases"


#: 原始用例（用户手写、还没拆成 YAML 的 txt）默认所在的两处。
#:
#: 为什么放在仓库外面：那是**用户自己的资料目录**，不是本工具的产物。
#: 工具只读它、导入一次，之后原文归数据库管（见 store.cases 表）。
_DEFAULT_RAW_CASE_DIRS = (Path(r"D:\dsh\cal_cases"), Path(r"D:\dsh\cases"))


def raw_case_dirs() -> list[Path]:
    """原始用例的扫描目录。不存在的直接跳过，不报错。

    ANDROID_RAW_CASE_DIRS 可以用 os.pathsep 分隔覆盖 —— 换机器、换目录时
    不用改代码。没设就用约定的那两个。
    """
    env = os.environ.get("ANDROID_RAW_CASE_DIRS")
    if env:
        cand = [Path(p.strip()) for p in env.split(os.pathsep) if p.strip()]
    else:
        cand = list(_DEFAULT_RAW_CASE_DIRS)
    return [p for p in cand if p.is_dir()]


def knowledge_dir() -> Path:
    return assets_dir() / "knowledge"


def where() -> str:
    """给人看的：当前用的是哪一个。排查时第一句就该看这个。"""
    try:
        return skill_root()
    except RuntimeError as e:
        return f"(解析失败) {e}"


if __name__ == "__main__":
    print("资产目录解析结果：")
    print(f"  skill_root : {where()}")
    print(f"  解释器     : {runner_python()}")
    try:
        r = assets_dir()
        for m in MARKERS:
            ok = (r / m).exists()
            print(f"    [{'✔' if ok else '✘'}] {m}")
        c = cases_dir()
        if c.is_dir():
            n = len(list(c.glob("*.yaml")))
            print(f"  用例 YAML  : {n} 个  ({c})")
        k = knowledge_dir()
        if k.is_dir():
            n = len(list(k.rglob('*.md')))
            print(f"  知识卡     : {n} 个")
    except RuntimeError as e:
        print(f"  {e}")
    print(f"\n  资产位置: {WORKSPACE_ASSETS}")
