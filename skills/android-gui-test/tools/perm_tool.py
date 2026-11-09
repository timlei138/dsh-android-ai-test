# -*- coding: utf-8 -*-
"""perm_tool：查 / 授 / 撤 App 的运行时权限 —— 绕开弹窗，直接用 adb 把前置条件铺好。

## 为什么需要这个工具（2026-11-06 用户要的）

权限弹窗是「点击的副作用」：点某个按钮才申请权限，框只在屏幕上活几秒。
`act` 的点击流程会自动点掉弹窗（见 `perm.handle`），但有两类情况它兜不住：

  1. **权限还没授予就要用到功能** —— 比如用例要「用已有账号同步日历」，
     而 READ_CALENDAR 还没授权，进去就是空列表。等点击再弹框已经晚了。
  2. **弹窗被系统直接吞掉** —— 用户第三次点「拒绝」后 Android 不再弹框
     （`permissioncontroller` 的 don't-ask-again 机制），点多少次都不会出现，
     AI 会误判成「控件坏了」。

这两种情况用 `dumpsys package` 一看就知道，用 `pm grant` 一条命令就解决 ——
不必去界面上斗智斗勇。

## 用法

```bash
# 查：当前所有运行时权限的授权状态
python tools/perm_tool.py --package com.zui.calendar

# 授：短名就够了，自动补 android.permission. 前缀
python tools/perm_tool.py --package com.zui.calendar --grant READ_CALENDAR,WRITE_CALENDAR

# 撤：测「拒绝路径」时用
python tools/perm_tool.py --package com.zui.calendar --revoke POST_NOTIFICATIONS
```

## 设计取舍

· **不自动跑**。用户裁决：「一个 case 查一次就够，这个就不写了吧，让 AI 自主决定。」
  所以这里只提供能力，什么时候用由执行子 Agent 按用例内容判断。
· **不解析 `pm list permissions`**（170ms，且要跨 App 对照）。`dumpsys package`
  220ms 一次就把这个包的运行时权限和 granted 状态全列出来了。
· **特权权限授权会失败**，报 `SecurityException: ... is managed by role`。
  原样放进 `failed` 交给 AI 看 —— 它能读懂那是什么意思，不需要我们翻译。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import apply_stdin_args, ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

PREFIX = "android.permission."

# `dumpsys package <pkg>` 里运行时权限段的样子：
#
#   runtime permissions:
#     android.permission.READ_CALENDAR: granted=true, flags=[ ... ]
#     android.permission.CAMERA: granted=false, flags=[ USER_SET ]
#
# 只认这一段 —— 上面还有 `requested permissions:` 和 `install permissions:`，
# 那些不是「运行时能授的」，列出来会误导。
_RUNTIME_BLOCK = re.compile(r"^\s*runtime permissions:\s*$", re.M)
_NEXT_BLOCK = re.compile(r"^\s{2}\S[^:]*:\s*$", re.M)
_PERM_LINE = re.compile(
    r"^\s*(android\.permission\.[A-Z0-9_]+):\s*granted=(true|false)", re.M)


def _full_name(name: str) -> str:
    """短名补全成 android.permission.XXX；已经是全名就原样返回。"""
    name = name.strip()
    if not name:
        return ""
    return name if name.startswith(PREFIX) else PREFIX + name


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [x for x in (_full_name(s) for s in value.split(",")) if x]


def runtime_permissions(serial: str | None, package: str) -> dict:
    """读 `dumpsys package`，返回这个包的运行时权限 → 是否已授予。

    返回 {"ok": True, "perms": {name: granted}} 或 {"ok": False, "error": msg}
    """
    try:
        import adbutils
        from common import _adb_server_alive
        if not _adb_server_alive():
            return {"ok": False, "error": "adb server 没起：先跑一次 `adb devices`"}
        dev = (adbutils.adb.device(serial) if serial
               else adbutils.adb.device())
        out = dev.shell(f"dumpsys package {package}")
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": f"查询失败：{error}"}

    # 只取 runtime permissions: 到下一个两空格缩进的段标题之间
    m = _RUNTIME_BLOCK.search(out)
    if not m:
        # 包不存在 / 还没被安装过 → dumpsys 里根本没这段
        return {"ok": True, "perms": {}}
    rest = out[m.end():]
    nxt = _NEXT_BLOCK.search(rest)
    block = rest[:nxt.start()] if nxt else rest
    perms = {name: (val == "true") for name, val in _PERM_LINE.findall(block)}
    return {"ok": True, "perms": perms}


def apply(serial: str | None, package: str, grant: list[str],
          revoke: list[str]) -> dict:
    """逐条 pm grant / pm revoke。返回成功和失败两摞。"""
    done, failed = [], []
    try:
        import adbutils
        dev = (adbutils.adb.device(serial) if serial
               else adbutils.adb.device())
    except Exception as error:  # noqa: BLE001
        return {"granted_via_adb": [], "revoked_via_adb": [],
                "failed": [{"error": f"设备连接失败：{error}"}]}

    def run(verb: str, name: str) -> None:
        cmd = f"pm {verb} {package} {name}"
        try:
            out = (dev.shell(cmd) or "").strip()
        except Exception as error:  # noqa: BLE001
            failed.append({"permission": name, "command": cmd,
                           "error": str(error)})
            return
        # pm 失败也是 exit 0，得看输出里有没有异常字样
        if out and ("Exception" in out or "Error" in out or "error" in out):
            # 只留最有信息量的那一行 —— 后面 30 行是 Java 堆栈，AI 读了
            # 没有任何用。挑「XxxException: 原因」，那句直接把原因说完。
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            head = next((ln for ln in lines if "Exception:" in ln),
                        lines[0] if lines else out)
            failed.append({"permission": name, "command": cmd, "error": head})
        else:
            done.append(name)

    for name in grant:
        run("grant", name)
    granted_via_adb = list(done)
    done.clear()
    for name in revoke:
        run("revoke", name)
    revoked_via_adb = list(done)
    return {"granted_via_adb": granted_via_adb,
            "revoked_via_adb": revoked_via_adb, "failed": failed}


def main() -> None:
    p = argparse.ArgumentParser(
        description="查/授/撤 App 的运行时权限（走 adb，不经过界面弹窗）")
    p.add_argument("--package", required=True, help="被测 App 的包名")
    p.add_argument("--grant", default=None,
                   help="要授予的权限，逗号分隔；短名会自动补 android.permission.")
    p.add_argument("--revoke", default=None,
                   help="要撤销的权限，逗号分隔；短名会自动补 android.permission.")
    p.add_argument("--serial", default=None,
                   help="设备 serial；只有一台设备时可省略")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    apply_stdin_args()
    args = p.parse_args()

    grant, revoke = _split(args.grant), _split(args.revoke)

    result: dict = {"package": args.package, "ok": True}
    if grant or revoke:
        result.update(apply(args.serial, args.package, grant, revoke))

    snap = runtime_permissions(args.serial, args.package)
    if not snap.get("ok"):
        result["ok"] = False
        result["error"] = snap.get("error")
    else:
        perms = snap["perms"]
        result["runtime_permissions"] = [
            {"name": n, "granted": g} for n, g in sorted(perms.items())]
        result["granted_count"] = sum(1 for g in perms.values() if g)
        result["total_count"] = len(perms)

    if args.json:
        import json
        print(json.dumps(result, ensure_ascii=False))
        return

    # 人读格式
    if not result.get("ok"):
        print(f"❌ {result.get('error')}")
        return
    print(f"包名：{args.package}")
    if grant:
        print(f"  已授予：" + (", ".join(result.get("granted_via_adb") or []) or "(无)"))
    if revoke:
        print(f"  已撤销：" + (", ".join(result.get("revoked_via_adb") or []) or "(无)"))
    for f in result.get("failed") or []:
        print(f"  ⚠️ 失败 {f.get('permission')}: {f.get('error')}")
    print(f"运行时权限（{result.get('granted_count')}/{result.get('total_count')} 已授予）：")
    for item in result.get("runtime_permissions") or []:
        mark = "✅" if item["granted"] else "❌"
        short = item["name"].replace(PREFIX, "")
        print(f"  {mark} {short}")


if __name__ == "__main__":
    main()
