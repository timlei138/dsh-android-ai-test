#!/usr/bin/env python3
r"""run.py —— 用 venv 的解释器跑工具层脚本。

## 为什么需要它

Windows PowerShell 5.1 有两个坑，每个新会话都踩：

  ① 引号包裹的 exe 必须加 `&`，否则解析失败
       "D:\\...\\python.exe" "D:\\...\\env.py"      ❌ Unexpected token
       & "D:\\...\\python.exe" "D:\\...\\env.py"    ✅

  ② 中文长文本里的 `“ ”` 被当成字符串定界符，内容被截断

## 怎么用

    "<target>\tools\run.py" env.py
    "<target>\tools\run.py" session.py start --title "168" --input @case.txt
    "<target>\tools\run.py" act.py tap --x 100 --y 200 --via "rid=btn_ok"

（PowerShell 里调 .py 文件不需要 `&`，因为它是通过文件关联走 python。
 但更稳的写法是显式给解释器，见下面「保险写法」。）

## 保险写法

如果 `run.py` 直接跑不起来（没配 .py 关联），用这个：

    & "<target>\.venv\Scripts\python.exe" "<target>\tools\run.py" env.py

## 它做了什么

  · 找到同目录的 venv 解释器（`../.venv/Scripts/python.exe`）
  · 强制 UTF-8 环境变量
  · 把剩下的参数原样转给目标脚本
  · 透传退出码
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent


def find_python() -> Path:
    """venv 解释器。找不到就退回当前解释器。"""
    for name in ("python.exe", "python"):
        p = ROOT / ".venv" / "Scripts" / name
        if p.is_file():
            return p
        p = ROOT / ".venv" / "bin" / name
        if p.is_file():
            return p
    return Path(sys.executable)


def main() -> int:
    # Windows 控制台默认 GBK，中文输出会崩 —— 强制 UTF-8
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    argv = sys.argv[1:]
    if not argv:
        print("用法: run.py <脚本> [参数...]", file=sys.stderr)
        print("例:   run.py env.py", file=sys.stderr)
        print("      run.py session.py start --title 168 --input @case.txt",
              file=sys.stderr)
        print(file=sys.stderr)
        print("可用脚本:", file=sys.stderr)
        for p in sorted(TOOLS.glob("*.py")):
            print(f"  {p.name}", file=sys.stderr)
        return 2

    script = argv[0]
    target = TOOLS / script
    if not target.is_file():
        # 允许写 act.py / act 两种
        alt = TOOLS / f"{script}.py"
        if alt.is_file():
            target = alt
        else:
            print(f"找不到脚本: {target}", file=sys.stderr)
            print("可用:", ", ".join(p.name for p in sorted(TOOLS.glob('*.py'))),
                  file=sys.stderr)
            return 2

    py = find_python()
    # ★ 用列表传参，不拼字符串 —— 引号/空格/中文全部原样过去
    cmd = [str(py), str(target)] + argv[1:]
    try:
        proc = subprocess.run(cmd, env=os.environ)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 1
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
