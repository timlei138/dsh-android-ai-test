# -*- coding: utf-8 -*-
"""env：一条命令告诉你"在这台机器上命令该怎么写"。

## 为什么需要独立的 env.py

`session.py start` 的返回里已经带 `env` 了，但那是**执行子 Agent** 的入口。
**主 Agent 从不调 session start**（它只派发、查用例、汇总），
所以拿不到那个字段 —— 于是它继续按 Linux 习惯敲 `ls -1`。

实测：主 Agent 问"有多少 case"，第一条命令就是
    ls -1 "D:/Project/deepseek/android-ai-test-skills/assets/cases"
在 Windows PowerShell 上 `ls` 不认 `-1`，直接 exit 1。

## 用法

```bash
python tools/env.py           # 人读的提示
python tools/env.py --json    # 给程序读
```

## 设计取舍：为什么不让 AI 自己探测

AI 自己写 `uname -s` 或 `python -c "import sys..."` 也能探，
但那要**多一次工具往返**，而且它经常懒得探就直接敲命令。

一条 `python tools/env.py` 比"先想想要不要探测"更省。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import apply_stdin_args, ensure_utf8_stdio  # noqa: E402
from runtime import (  # noqa: E402
    ASSETS_DIR, KNOWLEDGE_DIR, RECORD_DIR, ROOT, STORAGE_DIR, TMP_DIR,
    detect_shell, storage_fix_hint, storage_root_source, writability_report,
)

# 必须第一件事就切 UTF-8。
#
#   Windows 控制台默认 GBK，`print("⚠️ ...")` 会直接抛
#       UnicodeEncodeError: 'gbk' codec can't encode character '\u26a0'
#   整个脚本崩掉 → AI 拿不到 platform / shell / storage 提示 → 只能自己瞎试
#   （敲 Linux 命令、猜路径、反复重启 webui），一条用例还没开始就烧掉几十次工具调用。
ensure_utf8_stdio()


def _deps_ok() -> str:
    """关键依赖在不在？返回 "ok" 或缺失列表。

    为什么 env.py 要报这个：AI 拿到 `python` 路径后直接用它跑命令，
    但那个解释器可能一个依赖都没有（比如刚建的空 venv）。
    提前说清，比跑到 act.py 才 ImportError 好。
    """
    import importlib.util
    need = ("uiautomator2", "adbutils", "yaml", "PIL")
    miss = [m for m in need if not importlib.util.find_spec(m)]
    return "ok" if not miss else "缺: " + ",".join(miss)


IME_OK = "com.github.uiautomator/.AdbKeyboard"


def _storage_source() -> str:
    """storage/ 是按哪条规则定下来的 —— 和 runtime._resolve_storage_root() 的三档对应。

    为什么要报出来：AI 看到「storage 在哪」才知道产物落哪；看到「按哪条规则定的」
    才能在换机器 / 换 cwd 之后判断"这个路径对不对"，而不是等到写不进去才怀疑工具坏了。
    """
    return storage_root_source()


def _roots_agree() -> tuple[bool, str]:
    """脚本被调用时走的那条路径，和 runtime 算出来的 ROOT，是不是同一个地方？

    不一致 = 中间有软链 / junction。这本身是**允许的用法**
    （Windows 那边就靠 junction 把 skill 目录链进源码树），
    但产物根会跟着 resolve() 跑到链接目标去，资产也可能落在另一棵树里。
    所以不是禁止，而是必须说出来 —— 不说，跑完才发现 steps.yaml 不见了。
    """
    invoked = Path(__file__).resolve().parent.parent
    if invoked == ROOT:
        return True, ""
    return False, (f"调用路径 {Path(__file__).parent} 解析后是 {invoked}，"
                   f"而工具层 ROOT = {ROOT}（中间有软链 / junction）")


def _device() -> dict:
    """设备 + 输入法状态。

    ## 为什么放在 env 里（2026-11-06 定）

    写字的动作（`android_act input`）底层是 u2 的 `send_keys`，它要设备上
    当前输入法是 **AdbKeyboard**。换成搜狗/原生输入法后 `clear=True` 会抛

        AdbBroadcastError: ADB_KEYBOARD_CLEAR_TEXT failed: Attempt to read
        from field 'ExtractedText.text' on a null object reference

    这个报错出现在 act 的输出里，执行子 Agent 很容易误判成「控件坏了」，
    然后按「同一目标最多试 2 次」记 BLOCKED —— 实际是设备环境没准备。

    输入法在整个测试过程中不会被人改，所以**只查一次就够**：主 Agent 的
    开工第一条就是 `android_env`，在这里报出来，派发前就能发现并修掉。
    子 Agent 不用管这件事。

    ## 失败不能让 env.py 挂

    env.py 是开工第一条，它崩了整个流程就断了。所以设备不在线 / 多台设备
    / adb 起不来，一律返回 serial=None + advice，不抛异常，也不拖慢：
    查询前先探 5037（100ms），server 没跑就直接返回，不在这里起 daemon。
    """
    out = {"serial": None, "ime": None, "ime_ok": False, "advice": None}
    try:
        import adbutils
        from common import _adb_server_alive
        if not _adb_server_alive():
            out["advice"] = "adb server 没起：在 bash 里跑一次 `adb devices`"
            return out
        devices = [d.serial for d in adbutils.adb.list() if d.state == "device"]
        if not devices:
            out["advice"] = "设备没连：`adb devices` 确认是 device 态"
            return out
        if len(devices) > 1:
            out["advice"] = ("多台设备在线（" + "、".join(devices) +
                             "）：开工时显式指定 serial")
            return out
        serial = devices[0]
        out["serial"] = serial
        ime = adbutils.adb.device(serial).shell(
            "settings get secure default_input_method").strip()
        out["ime"] = ime or None
        if ime == IME_OK:
            out["ime_ok"] = True
        else:
            out["advice"] = (f"当前输入法是 {ime or '(空)'}，写字会失败。"
                             f"切回：adb -s {serial} shell ime enable {IME_OK}"
                             f" ; adb -s {serial} shell ime set {IME_OK}")
        return out
    except Exception as error:  # noqa: BLE001
        out["advice"] = f"设备查询失败：{error}"
        return out


def main() -> None:
    # ★ 必须在判断 --json 之前调：插件走 `env.py --stdin-json` 把参数塞 stdin，
    #   apply_stdin_args() 会把 sys.argv 换成 JSON 里的参数（可能含 --json）。
    #   放后面就会出现「AI 传了 --json 却拿到人类可读文本」的老问题。
    apply_stdin_args()
    env = detect_shell()
    _writ = writability_report()
    _root_ok, _root_note = _roots_agree()
    if "--json" in sys.argv:
        payload = dict(env)
        payload.update({
            # ★ 这条最关键：**用哪个解释器跑后面的命令**
            #
            #   不能写死在 skill 里，因为"哪个 venv 装了依赖"是运行时状态：
            #     本机：安装位置的 venv 有依赖，target 的是空的
            #     别人：可能反过来（自己装过依赖之后）
            #
            #   所以由这里报出来，AI 拿到什么就用什么 —— 换机器自动跟着变。
            "python": sys.executable,
            "tools_dir": str(Path(__file__).resolve().parent),
            "storage_dir": str(STORAGE_DIR),
            "record_dir": str(RECORD_DIR),
            "tmp_output": str(TMP_DIR),
            "knowledge_dir": str(KNOWLEDGE_DIR),
            "deps_ok": _deps_ok(),
            # ★ 可写性实测：权限位好看 ≠ 写得进去（沙箱就是这么拦的）
            "writability": _writ,
            "storage_writable": not _writ["storage"],
            "assets_writable": not (_writ["knowledge"] or _writ["cases"]),
            "tool_root": str(ROOT),
            "roots_agree": _root_ok,
            "root_note": _root_note,
            # ★ 设备和输入法：主 Agent 开工就看到，别等子 Agent 跑到打字那步才炸
            "device": _device(),
            "storage_source": _storage_source(),
        })
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"platform : {env['platform']}")
    print(f"shell    : {env['shell']}")
    print(f"tmp_dir  : {env['tmp_dir']}")
    print()
    print(env["hint"])
    print()

    # ── 产物目录：这个必须打出来，AI 才能确认它对不对 ────────────
    src = _storage_source()
    setup_cmd = ("pwsh -File scripts\\setup.ps1" if os.name == "nt"
                 else "bash scripts/setup.sh")
    print("★ 用这个解释器跑后面的所有命令：")
    print(f"  {sys.executable}")
    deps = _deps_ok()
    if deps != "ok":
        print(f"  ⚠️ 但它{deps} —— 先跑 {setup_cmd} 装依赖")
    print()
    print("运行产物目录（来源：%s）：" % src)
    print(f"  storage  = {STORAGE_DIR}")
    print(f"  record   = {RECORD_DIR}")
    print(f"  tmp      = {TMP_DIR}")
    print(f"  knowledge= {KNOWLEDGE_DIR}")
    print(f"  assets   = {ASSETS_DIR}")
    print()
    # ── 可写性：真写一个探针，别看权限位 ──────────────────────
    hard_keys = ("storage", "run_history", "tmp")
    hard = [k for k in hard_keys if _writ.get(k)]
    soft = [k for k, v in _writ.items() if v and k not in hard_keys]
    if not hard and not soft:
        print("可写性：storage / assets 全部实测可写 ✅")
    else:
        print("⚠️ 可写性实测（权限位是 rwx 也照样可能被沙箱拦）：")
        for k in hard:
            print(f"  ✗ {k:<12} {_writ[k]}")
        for k in soft:
            print(f"   ! {k:<12} {_writ[k]}")
        if hard:
            print("  → 上面硬失败：session.py start 会直接拒绝开始（不会偷偷换个库）。")
        if soft:
            print("  → 上面软失败：跑得不影响，但 steps.yaml / 知识卡写不回，下次重摸路。")
        print(f"  → 解法：{storage_fix_hint()}")
    if not _root_ok:
        print(f"⚠️ 路径不同源：{_root_note}")
    print()

    # ── 设备 + 输入法 ───────────────────────────────────────────
    dev = _device()
    print("★ 设备：")
    if dev["serial"]:
        print(f"  serial   = {dev['serial']}")
        mark = "✅" if dev["ime_ok"] else "❌"
        print(f"  输入法   = {dev['ime'] or '(空)'} {mark}")
    if dev["advice"]:
        print(f"  ⚠️ {dev['advice']}")
    print()

    # ── 工具目录 + 开会话模板 ────────────────────────────────────
    #
    # 工具在哪、怎么开会话都直接印出来，省掉 AI 自己探路的那几步。
    #
    # ⚠️ 不能用 Path(__file__).resolve() —— 它会**解开 junction**：
    #   skill 装在 ~/.agents/skills/android-gui-test，
    #   是个指向源码 D:\Project\...\android-ai-test-skills 的链接。
    #   resolve() 之后报的是源码路径，而 AI 该用的是工作区那份
    #   （有 .venv、有 storage）。所以优先用工作区根目录。
    here = Path(__file__).parent          # 不 resolve，保留 junction 路径
    ws_root = STORAGE_DIR.parent          # <target>（storage 的上一级）
    tools_dir = ws_root / "tools"
    if not (tools_dir / "session.py").is_file():
        tools_dir = here                  # 回退：用脚本所在目录
    py = sys.executable
    print("★ 工具目录（不用再找）：")
    print(f"  tools    = {tools_dir}")
    print(f"  target   = {ws_root}")
    print()
    print("★ 开会话（唯一必须用 bash 的命令）：")
    if os.name == "nt":
        print(f'  & "{py}" "{tools_dir}\\session.py" start '
              f'--title "<标题>" --input "@<用例文件>" --package <包名>')
    else:
        print(f'  "{py}" "{tools_dir}/session.py" start \\'
              f'\n    --title "<标题>" --input "@<用例文件>" --package <包名>')
    print("  中文长文本先写 UTF-8 文件再传 @文件；别直接放命令行")
    print()
    # 只有真写不进的时候才提示换路径；能写就不啰嗦（上面已实测过）
    if any(_writ.get(k) for k in ("storage", "run_history", "tmp")):
        print("⚠️ 上面这几个目录写不进去 —— 先设环境变量再调工具：")
        print()
        print(f"   {storage_fix_hint()}")
        print()
        print("   设完重跑这条命令，确认上面的 storage 变了。")
        print()

    if env["shell"] == "powershell":
        print("本机常用对照：")
        print("  ls -la dir        ->  dir dir   /  Get-ChildItem dir")
        print("  ls -1 dir         ->  dir dir   （PowerShell 的 ls 不认 -1）")
        print("  head -20 f        ->  Get-Content f -TotalCount 20")
        print("  cmd1 && cmd2      ->  cmd1; cmd2")
        print("  cmd 2>/dev/null   ->  cmd 2>$null")
        print("  cat f             ->  Get-Content f")
        print("  which python      ->  Get-Command python")
        print()
        print("★ 但 `adb shell \"...\"` 里的内容仍是 Android 的 Linux，照常写 grep 等。")


if __name__ == "__main__":
    main()
