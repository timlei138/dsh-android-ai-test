"""state：设备状态原语（权限/数据/应用生命周期），不动屏幕内容。

权限测试的第一选择：测"已授权/已拒绝"路径时用 grant/revoke 预置状态，
弹窗根本不出现；只有测弹窗交互本身（用户点允许/拒绝那一刻的行为）才需要
act --watch 的弹窗内点击。全部操作自动记入当前会话事件流。

用法：
  python state.py grant  --package com.app --perm android.permission.CAMERA
  python state.py revoke --package com.app --perm android.permission.CAMERA
  python state.py clear  --package com.app        # ⚠️ 清空应用全部数据
  python state.py start  --package com.app [--activity .MainActivity]
  python state.py stop   --package com.app
"""
from __future__ import annotations

import argparse

from common import (add_common_args_all_subcommands, connect, emit, fail,
                    log_auto, guard_terminated)
from runtime import execution

PERMS = {
    "camera": "android.permission.CAMERA",
    "location": "android.permission.ACCESS_FINE_LOCATION",
    "coarse_location": "android.permission.ACCESS_COARSE_LOCATION",
    "mic": "android.permission.RECORD_AUDIO",
    "contacts": "android.permission.READ_CONTACTS",
    "storage": "android.permission.READ_EXTERNAL_STORAGE",
    "media_images": "android.permission.READ_MEDIA_IMAGES",
    "notifications": "android.permission.POST_NOTIFICATIONS",
}


def resolve_perm(v: str) -> str:
    """支持短名（camera/location/...）与全名两种写法。"""
    return PERMS.get(v, v)


def _perm_granted(d, pkg: str, perm: str) -> bool:
    """回读 dumpsys 判断权限是否 granted（pm grant/revoke 成功不一定有输出，
    用空输出判成功会谎报——设备抖动时 output 可能为空）。

    dumpsys 全文取回本地解析：设备端 grep 在部分 ROM 上不存在或 -w 行为不一。
    """
    out = (d.shell(f"dumpsys package {pkg}").output or "")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(perm) and "granted=" in line:
            return "granted=true" in line
    return False


def main() -> None:
    p = argparse.ArgumentParser(description="state：设备状态原语")
    p.add_argument("--serial", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("grant", "revoke"):
        s = sub.add_parser(name)
        s.add_argument("--package", required=True)
        s.add_argument("--perm", required=True, help=f"短名或全名，短名: {', '.join(PERMS)}")

    s = sub.add_parser("clear")
    s.add_argument("--package", required=True)

    s = sub.add_parser("start")
    s.add_argument("--package", required=True)
    s.add_argument("--activity", default=None, help="可选，相对 activity 名")

    s = sub.add_parser("stop")
    s.add_argument("--package", required=True)

    # --serial 写在子命令前后都能解析（与其他工具一致）
    add_common_args_all_subcommands(p)
    args = p.parse_args()
    guard_terminated(f"state {args.cmd}")
    try:
        d = connect(args.serial)
    except Exception as e:  # noqa: BLE001
        fail(f"设备连接失败: {e}")

    execution("may_have_executed")
    try:
        if args.cmd in ("grant", "revoke"):
            perm = resolve_perm(args.perm)
            out = (d.shell(f"pm {args.cmd} {args.package} {perm}").output or "").strip()
            granted = _perm_granted(d, args.package, perm)
            ok = granted if args.cmd == "grant" else not granted
            detail = f"pm {args.cmd} {args.package} {perm} → {'granted' if granted else 'not granted'}"
            execution("executed")
            log_auto("state", f"state {args.cmd}", detail,
                     {"perm": perm, "ok": ok, "verified": True})
            if ok:
                hint = "预置状态成功，触发该权限的操作不会再弹窗"
            elif "Exception" in out or "Error" in out:
                # 结构化返回而非 sys.exit：AI 拿到 JSON 自行决策（弹窗路径/换权限）
                hint = f"⚠️ pm {args.cmd} 被拒绝: {out[:200]}；改走弹窗路径（act --watch）"
            else:
                hint = ("⚠️ 回读校验未达预期（granted=" + str(granted) + "）——预置未生效，"
                        "该权限可能不是 runtime 权限或设备策略限制；"
                        "改走弹窗路径（act --watch），不要对未预置的状态做判定")
            emit(ok, {"cmd": args.cmd, "perm": perm, "output": out,
                      "granted": granted, "verified": True, "hint": hint})
            return
        elif args.cmd == "clear":
            out = (d.shell(f"pm clear {args.package}").output or "").strip()
            ok = "Success" in out
            execution("executed")
            log_auto("state", "state clear", f"pm clear {args.package}", {"ok": ok})
            emit(ok, {"output": out, "hint": "应用已回到首次使用态（含外部 Android/data）"})
        elif args.cmd == "start":
            comp = f"{args.package}/{args.activity}" if args.activity else args.package
            d.app_start(args.package, args.activity) if args.activity else d.app_start(args.package)
            execution("executed")
            log_auto("state", "state start", f"启动 {comp}", {"ok": True})
            emit(True, {"started": comp, "hint": "冷启动后旋转锁定可能被解锁，注意 rotation"})
        elif args.cmd == "stop":
            d.app_stop(args.package)
            execution("executed")
            log_auto("state", "state stop", f"force-stop {args.package}", {"ok": True})
            emit(True, {"stopped": args.package})
    except Exception as e:  # noqa: BLE001
        fail(f"state {args.cmd} 失败: {e}")


if __name__ == "__main__":
    main()
