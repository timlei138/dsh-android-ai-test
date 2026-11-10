#!/usr/bin/env bash
# 转发到 install.py。环境判定和安装步骤只留一份实现。
#
# 为什么不再是两份 shell：
#   这套东西曾经同时有 install.sh 和 install.ps1，而 install.ps1 在这台机器上
#   根本没有 pwsh，**从没被执行过、语法都没验过**；"工作区在哪、这个目录写得进吗"
#   这么一条判断写了两遍，就必然对不齐（2026-11-09 为对齐它踩了一整天）。
#   系统 python3 本来就是前置条件（建 venv 要用），那就让它当唯一入口。
#
# 用法全在 install.py 里：bash install.sh --help
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "  ✗ 找不到 python3 / python —— 它本来就是这套东西的前置条件（建 venv 要用）。" >&2
    echo "    装一个 Python 3.10+ 再跑；已经装过层、只是补依赖的话：bash $DIR/toolkit/scripts/setup.sh" >&2
    exit 1
fi
[ -f "$DIR/install.py" ] || { echo "  ✗ 缺 $DIR/install.py（包不完整）" >&2; exit 1; }
exec "$PY" "$DIR/install.py" "$@"
