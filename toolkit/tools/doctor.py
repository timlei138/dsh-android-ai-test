#!/usr/bin/env python3
"""doctor：装完 / 换机器 / 换会话策略之后，一条命令自证这套东西能落盘。

为什么要有它
------------
2026-11-09 上午实测踩过：`session.py start` 报一句 `PermissionError: [Errno 13]`，
现场没有任何信息说明"是产物目录写不进去"。排查绕了一大圈（查属主、查挂载、
查 namei、最后才发现是 harness 会话的**文件沙箱**在拦）。
根因有两层，都得治：

  1. 工具层原来把失败咽掉了 —— runtime 启动时建目录是 `except OSError: pass`。
  2. 就算抛了，也只说"写不进"，不说"按哪条规则定的这个路径"和"该怎么改"。

所以 doctor 只做一件事：**把每一条判断标准摊开成行，逐行实测，给可执行的修法。**

用法
----
    python tools/doctor.py            # 人读
    python tools/doctor.py --json     # 程序读
    python tools/doctor.py --package com.zui.calendar   # 顺带查这个包的用例/知识卡

退出码：0 = 可以开跑；1 = 有警告但能跑；2 = 有硬失败，别开始。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime import (  # noqa: E402
    ASSETS_DIR, CASES_DIR, KNOWLEDGE_DIR, RECORD_DIR, ROOT, STORAGE_DIR, TMP_DIR,
    probe_writable, storage_fix_hint, storage_root_source, writability_report,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

HARD, SOFT, INFO = "FAIL", "WARN", "OK"
DEPS = ("uiautomator2", "adbutils", "yaml", "PIL")
MIN_PY = (3, 10)

_rows: list[dict] = []


def check(level: str, name: str, detail: str, fix: str = "") -> str:
    _rows.append({"level": level, "name": name, "detail": detail, "fix": fix})
    return level


def _py_ok() -> None:
    v = sys.version_info
    if (v.major, v.minor) < MIN_PY:
        check(HARD, "python 版本", f"{v.major}.{v.minor} < {'.'.join(map(str, MIN_PY))}",
              "换 3.10+ 的解释器，或用那个解释器重建 .venv")
    else:
        check(INFO, "python 版本", f"{v.major}.{v.minor}  {sys.executable}")


def _deps() -> None:
    miss = [m for m in DEPS if not importlib.util.find_spec(m)]
    if miss:
        check(HARD, "python 依赖", "缺 " + ",".join(miss),
              '装依赖（国内记得带镜像）：'
              + (f'& "{sys.executable}" -m pip install -r requirements.txt'
                 if os.name == "nt"
                 else f'"{sys.executable}" -m pip install -r requirements.txt'))
    else:
        check(INFO, "python 依赖", ",".join(DEPS) + " 全在")


def _roots() -> None:
    """ROOT / 调用路径 / cwd 三者关系。中间有软链不算错，但必须说出来。"""
    invoked = Path(__file__).resolve().parent.parent
    if invoked != ROOT:
        check(SOFT, "工具层路径同源",
              f"调用路径解析后 {invoked} ≠ ROOT {ROOT}（中间有软链 / junction）",
              "不是错误，但 steps.yaml / 知识卡会写到 ROOT 那一侧；"
              "确认那是你要的树（详见安装说明「装在哪」一节）")
    else:
        check(INFO, "工具层路径同源", str(ROOT))
    src = storage_root_source()
    check(INFO, "storage 来源规则", f"{src}  →  {STORAGE_DIR}")
    # 规则 ③ 触发时产物会落到 cwd，和工具层分家 —— 今天就是这么废掉一次运行
    if STORAGE_DIR.parent != ROOT:
        check(SOFT, "产物与工具层同树",
              f"storage 在 {STORAGE_DIR.parent}，工具层在 {ROOT}：库和资产分在两棵树",
              f"cd 到工具层根再跑（cwd={Path.cwd()}）；或固定一个可写位置："
              f"{storage_fix_hint(ROOT / 'storage')}")
    else:
        check(INFO, "产物与工具层同树", str(ROOT))


def _writ() -> bool:
    report = writability_report()
    hard_keys = ("storage", "run_history", "tmp")
    bad_hard = [k for k in hard_keys if report.get(k)]
    bad_soft = [k for k, v in report.items() if v and k not in hard_keys]
    if bad_hard:
        for k in bad_hard:
            check(HARD, f"可写性 {k}", f"{report[k]}  ({STORAGE_DIR})",
                  "写不进去就别开始：① 把工具层装进会话工作区内 "
                  f"② {storage_fix_hint()} ③ 放宽会话文件策略到 workspace-write")
    if bad_soft:
        for k in bad_soft:
            check(SOFT, f"可写性 {k}", report[k],
                  "跑得住但写不回资产：steps.yaml / 知识卡会静默丢失，下次重摸路")
    if not bad_hard and not bad_soft:
        check(INFO, "可写性", "storage / run_history / tmp / knowledge / cases 全部实测可写")
    return not bad_hard


def _db() -> None:
    db = STORAGE_DIR / "test.db"
    if not db.is_file():
        check(INFO, "运行库", "还没有 test.db（首次运行会自动建）")
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = con.execute("select count(*) from sessions").fetchone()[0]
        con.close()
        check(INFO, "运行库", f"{db}  sessions={n}  {db.stat().st_size // 1024} KB")
    except sqlite3.Error as exc:
        check(HARD, "运行库", f"打不开：{exc}", "先备份再手工修；别在坏的库上继续跑")


def _device() -> None:
    adb = shutil.which("adb")
    if not adb:
        check(HARD, "adb", "PATH 里没有 adb", "装 platform-tools 并把 adb 放进 PATH")
        return
    try:
        out = subprocess.run([adb, "get-state"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        check(HARD, "设备", f"adb get-state 失败：{exc}", "确认已开 USB 调试并授权本机")
        return
    if out == "device":
        check(INFO, "设备", "在线（adb get-state = device）")
    else:
        check(SOFT, "设备", f"adb get-state = {out or '(空)'}",
              "跑之前把设备连上；工具层不会替你修 adb server")
        return
    try:
        ime = subprocess.run(
            [adb, "shell", "settings", "get", "secure", "default_input_method"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        ime = ""
    if "uiautomator" in ime:
        check(INFO, "输入法", ime + "（写字可用）")
    else:
        check(SOFT, "输入法", ime or "(读不到)",
              "打字会失败：adb shell ime enable/set com.github.uiautomator/.AdbKeyboard")


def _assets(package: str | None) -> None:
    if not CASES_DIR.is_dir():
        check(SOFT, "用例目录", f"{CASES_DIR} 不存在", "跑 setup 建目录，或拷入用例资产")
        return
    pkgs = [p for p in CASES_DIR.iterdir() if p.is_dir()]
    check(INFO, "用例库", f"{CASES_DIR}  包目录 {len(pkgs)} 个："
          + ", ".join(f"{p.name}({len(list(p.glob('*.md')))} md/"
                      f"{len(list(p.glob('*_steps.yaml')))} steps)" for p in pkgs))
    if package:
        d = CASES_DIR / package
        if not d.is_dir():
            check(SOFT, "指定包用例", f"{d} 不存在", "确认包名拼写，或该包还没导入用例")
        for card in (KNOWLEDGE_DIR / f"{package}.md", KNOWLEDGE_DIR / "paths" / f"{package}.md"):
            check(INFO if card.is_file() else SOFT, "知识卡",
                  f"{card} " + ("在" if card.is_file() else "缺（首次跑会自动长出来）"))


def main() -> int:
    p = argparse.ArgumentParser(description="工具层自检")
    p.add_argument("--json", action="store_true")
    p.add_argument("--package", default=None, help="顺带检查这个包的用例与知识卡")
    args = p.parse_args()

    _py_ok()
    _deps()
    storage_ok = _writ()
    _roots()
    _db()
    _device()
    _assets(args.package)

    worst = HARD if any(r["level"] == HARD for r in _rows) else (
        SOFT if any(r["level"] == SOFT for r in _rows) else INFO)
    if args.json:
        print(json.dumps({"verdict": worst, "storage_dir": str(STORAGE_DIR),
                          "root": str(ROOT), "checks": _rows},
                         ensure_ascii=False, indent=2))
    else:
        mark = {HARD: "✗", SOFT: "!", INFO: "✓"}
        print(f"工具层自检  root={ROOT}")
        print(f"        cwd={Path.cwd()}\n")
        for r in _rows:
            print(f"  {mark[r['level']]} {r['name']:<14} {r['detail']}")
            if r["fix"]:
                print(f"      → {r['fix']}")
        print()
        if worst == HARD:
            print("✗ 有硬失败：现在开跑只会把时间花在看不懂的报错上。照上面 → 修完重跑本命令。")
        elif worst == SOFT:
            print("! 能跑，但上面这些 ! 项会让你少拿到产物（steps.yaml / 知识卡 / 打字）。")
        else:
            print("✓ 全部通过，可以开会话了。")
    return 2 if worst == HARD else (1 if worst == SOFT else 0)


if __name__ == "__main__":
    raise SystemExit(main())
