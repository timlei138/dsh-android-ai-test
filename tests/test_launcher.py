# -*- coding: utf-8 -*-
"""启动脚本的坑。

## 为什么要测启动脚本

这两个 bug 都不是"写错代码"，而是 Windows 特有的坑，
而且都表现为"看起来什么都没发生"：

  ① .ps1 没有 BOM 时，Windows PowerShell 按 GBK 读中文 ——
     中文注释被读成乱码，进而被当成命令，整脚本崩掉。
     症状：双击后窗口一闪就没了，什么也看不到。

  ② ANDROID_SKILL_ROOT 写成了仓库根，而不是 assets。
     store.db_path() 会往后拼 storage/runs.db，于是库落到
     仓库根的 storage/ 下，而写库那边用的是 assets/storage/。
     症状：跑完用例打开页面，一条记录都没有 —— 数据没丢，只是找错库。

两个都属于"静默错误"，只能靠测试盯住。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = FAIL = 0


def ck(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")


print("=" * 70)
print("1) .ps1 必须纯 ASCII")
print("=" * 70)

ps1 = ROOT / "start-webui.ps1"
ck("start-webui.ps1 存在", ps1.exists())
if ps1.exists():
    raw = ps1.read_bytes()
    has_bom = raw[:3] == b"\xef\xbb\xbf"
    body = raw[3:] if has_bom else raw
    try:
        body.decode("ascii")
        ascii_ok = True
    except UnicodeDecodeError:
        ascii_ok = False
    # 有 BOM 也行（PowerShell 会按 UTF-8 读），但更稳的是纯 ASCII ——
    # 编辑器重写文件时经常把 BOM 丢掉。
    ck("★ 没有非 ASCII 字符（否则中文被当 GBK 读成乱码）",
       ascii_ok,
       "文件里有非 ASCII 字节；Windows PowerShell 无 BOM 时按 GBK 解析会崩")
    ck("有 BOM 或纯 ASCII（二者至少满足一个）", ascii_ok or has_bom)

print()
print("2) .cmd 也必须纯 ASCII")
cmd = ROOT / "start-webui.cmd"
ck("start-webui.cmd 存在", cmd.exists())
if cmd.exists():
    try:
        cmd.read_bytes().decode("ascii")
        ok = True
        err = ""
    except UnicodeDecodeError as e:
        ok = False
        err = str(e)
    ck("★ .cmd 纯 ASCII（cmd.exe 按 ANSI 读）", ok, err)
    txt = cmd.read_text(encoding="ascii", errors="replace")
    ck("cmd 会转交给 ps1", "start-webui.ps1" in txt)
    ck("cmd 用 Bypass 绕过执行策略", "Bypass" in txt,
       "不加 -ExecutionPolicy Bypass，默认策略下 .ps1 会被拒绝")

print()
print("3) ANDROID_SKILL_ROOT 必须指向 assets，不是仓库根")
if ps1.exists():
    t = ps1.read_text(encoding="utf-8", errors="replace")
    # 找 ANDROID_SKILL_ROOT 被赋值的那一行
    m = re.search(r"ANDROID_SKILL_ROOT\s*=\s*(.+)", t)
    ck("脚本里设了 ANDROID_SKILL_ROOT", m is not None)
    if m:
        val = m.group(1).strip()
        ck("★ 设的是 assets 而不是仓库根（否则库会落错地方）",
           "assets" in val,
           f"实际: {val}")

print()
print("4) webui.py 自己也要兜底（不依赖启动脚本）")
import os  # noqa: E402

saved = os.environ.get("ANDROID_SKILL_ROOT")
try:
    os.environ.pop("ANDROID_SKILL_ROOT", None)
    sys.path.insert(0, str(ROOT))
    import importlib
    import webui
    importlib.reload(webui)
    root = Path(os.environ["ANDROID_SKILL_ROOT"])
    ck("★ 没设时自动补成 assets", root.name == "assets", str(root))

    # 设成仓库根这种错值，应该被纠正
    os.environ["ANDROID_SKILL_ROOT"] = str(ROOT)
    importlib.reload(webui)
    root2 = Path(os.environ["ANDROID_SKILL_ROOT"])
    ck("★ 设成仓库根会被纠正成 assets", root2.name == "assets", str(root2))
finally:
    if saved is None:
        os.environ.pop("ANDROID_SKILL_ROOT", None)
    else:
        os.environ["ANDROID_SKILL_ROOT"] = saved

print()
print("5) 重复启动要识别已开，而不是报端口占用")
webui_src = (ROOT / "webui.py").read_text(encoding="utf-8")
ck("有 _already_running 探测", "_already_running" in webui_src)
ck("启动前先探测", "if _already_running(" in webui_src)
ck("端口被占时给换端口的提示", "--port 8771" in webui_src)
ck("★ 启动信息 flush（否则重定向时什么都看不到）",
   "flush=True" in webui_src,
   "块缓冲会让人以为服务没起来，然后重复启动")

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
