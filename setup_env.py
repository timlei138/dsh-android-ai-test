"""一次性环境初始化：检查依赖，缺什么装什么。

## 为什么需要它

新人（或新机器、或换了 Python 版本）拿到这个仓库，第一件事就是装依赖。
以前这件事写在 README 里，靠人照着敲 —— 漏装一个，跑到一半才炸，
而且报错是"缺依赖"这种看不出该装什么的话。

用户的要求是：**Agent 第一跑的时候自己把环境装好**。
所以这个脚本被 run_case_json.py 在启动时调用，不需要人记得做。

## 它怎么判断"要不要装"

不去猜，直接 `import` 试。能 import 就是有，不能就是没有。
比读 requirements.txt 再比对版本可靠 —— 版本号对得上但 ABI 不兼容的情况
（比如装了一半、Python 版本变了）只有真 import 才试得出来。

## 为什么是"缺什么装什么"而不是"每次全装"

pip install 有网络开销（几十秒到几分钟），每次跑用例都来一遍不能接受。
所以：全都有 → 0.2 秒返回；缺一个 → 只装缺的那些。

## 安全边界

· 只在**确实缺**的时候碰网络
· 安装失败不吞异常，明确报出来并给出可操作的建议
· 装了新包之后**重新 import 验证**，不相信 pip 的退出码
  （踩过：pip 报成功但包其实没装到当前解释器里）
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

# 必需依赖：(import 名, pip 名)
#
# ⚠️ import 名和 pip 名不一定一样，必须分开写：
#    cv2 <- opencv-python，PIL <- pillow，yaml <- PyYAML
#    按 import 名前去 pip 搜会搜到别的包（cv2 在 PyPI 上是个无关的占位包）。
REQUIRED = [
    # 编排层：LangGraph 状态机
    ("langgraph", "langgraph"),
    ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite"),
    # 设备层：adb 通信与 UI 树
    ("adbutils", "adbutils"),
    ("uiautomator2", "uiautomator2"),
    # OCR：UI 树读不到的内容（toast / Canvas 自绘）
    ("rapidocr", "rapidocr>=3.0.0"),
    ("onnxruntime", "onnxruntime"),   # rapidocr 3.x 的默认推理引擎
    ("cv2", "opencv-python"),
    ("PIL", "pillow"),
    ("numpy", "numpy"),
    # 用例与网络
    ("yaml", "PyYAML"),
    ("requests", "requests"),
]

# 解释器要求。低于这个版本装不上上面某些包。
MIN_PY = (3, 10)

_REPO = Path(__file__).resolve().parent


def _missing() -> list[tuple[str, str]]:
    """返回装不上的那些 (import 名, pip 名)。

    ⚠️ 用 importlib.import_module 真试，不是查包列表：
       装了一半的包、或者包在但动态库加载失败，只有真 import 才发现。
    """
    out = []
    for imp_name, pip_name in REQUIRED:
        try:
            importlib.import_module(imp_name)
        except Exception:  # noqa: BLE001 —— 缺包、ABI 不符、依赖缺失都算
            out.append((imp_name, pip_name))
    return out


def _pip_install(pip_names: list[str]) -> bool:
    """装指定的包，返回是否成功。"""
    cmd = [sys.executable, "-m", "pip", "install", *pip_names]
    print(f"  $ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, timeout=1800)
    except subprocess.TimeoutExpired:
        print("  ❌ 安装超时（30 分钟）", file=sys.stderr)
        return False
    return r.returncode == 0


def ensure(verbose: bool = False) -> tuple[bool, list[str]]:
    """确保依赖齐备。返回 (是否可用, 装失败的名字)。

    可以被任何入口调用 —— run_case_json.py 在启动时就会调它。
    """
    # ---- 1. 解释器版本 ----
    if sys.version_info < MIN_PY:
        print(f"  ❌ Python {sys.version.split()[0]} 太旧，"
              f"需要 >= {MIN_PY[0]}.{MIN_PY[1]}", file=sys.stderr)
        return False, ["python"]

    # ---- 2. 缺什么 ----
    miss = _missing()
    if not miss:
        if verbose:
            print(f"  ✅ 依赖齐备（{len(REQUIRED)} 项检查通过）")
        return True, []

    print(f"\n🔧 缺 {len(miss)} 个依赖，开始安装……")
    for imp_name, pip_name in miss:
        print(f"     · {imp_name:34s} ← {pip_name}")
    print()

    # ---- 3. 装 ----
    ok = _pip_install([p for _, p in miss])
    if not ok:
        # 一次装全部失败时，再逐个试一次 —— 常见原因是某个包没有对应
        # Python 版本的 wheel，整批被它拖垮。逐个装能把"谁是罪魁"指出来。
        print("\n  整批安装失败，逐个重试以定位问题……", file=sys.stderr)
        bad = []
        for imp_name, pip_name in miss:
            if not _pip_install([pip_name]):
                bad.append(pip_name)
        if bad:
            print(f"\n  ❌ 装不上：{', '.join(bad)}", file=sys.stderr)
            return False, bad

    # ---- 4. 装完必须**重新验证** ----
    # 不能只信 pip 的退出码：踩过 pip 报成功、但包没进当前解释器的情况
    # （典型是装到了别的 Python 里、或者需要重启进程）。
    importlib.invalidate_caches()
    still = _missing()
    if still:
        # 有些包装完需要新进程才能 import（扩展模块的 .pth 等）。
        # 这里不强行重启，直接告诉人跑一次就够了。
        print(f"\n  ⚠️ 装完了但还有 {len(still)} 个 import 不到："
              f"{[n for n, _ in still]}", file=sys.stderr)
        print("     这通常新开一个进程就好了。", file=sys.stderr)
        return False, [p for _, p in still]

    print(f"\n  ✅ 环境就绪（装了 {len(miss)} 个依赖）")
    return True, []


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="检查并安装运行依赖（缺什么装什么）")
    ap.add_argument("--check", action="store_true",
                    help="只检查不安装，返回码 1 表示缺依赖")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    print("=" * 60)
    print("环境检查")
    print("=" * 60)
    print(f"  解释器 : {sys.executable}")
    print(f"  版本   : {sys.version.split()[0]}")
    print(f"  仓库   : {_REPO}")
    print()

    if args.check:
        miss = _missing()
        if miss:
            print(f"  缺 {len(miss)} 个：")
            for imp_name, pip_name in miss:
                print(f"     · {pip_name}")
            return 1
        print(f"  ✅ {len(REQUIRED)} 项全部就位")
        return 0

    ok, bad = ensure(verbose=args.verbose)
    if not ok:
        print()
        print("=" * 60)
        print("自动安装没能搞定，请手动处理：")
        print("=" * 60)
        print(f"  {Path(sys.executable).parent}\\python.exe -m pip install \\")
        print(f"      {' '.join(bad)}")
        print()
        print("  如果报「找不到匹配的版本」，多半是这个包不支持当前 Python 版本。")
        print("  排查：python -m pip index versions <包名>")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
