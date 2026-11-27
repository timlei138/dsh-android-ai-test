"""固定参数的精细系统手势：底部上划导航、底部左右滑动切换相邻应用。

python gesture.py show_taskbar --why "调出任务栏"
python gesture.py recents
python gesture.py home
python gesture.py switch_app_left
python gesture.py switch_app_right

只开放动作和公共取证参数；手势由工具按内部预设执行。
left/right 表示手指移动方向，用于切换相邻应用。
ok 表示手势和取证完成，applied=null 需结合本次截图确认目标界面。
"""
from __future__ import annotations

import argparse
import re
import time
from datetime import datetime

import checkpoint
from common import (add_common_args, apply_stdin_args, capture, connect, emit,
                    ensure_utf8_stdio, evidence_paths, fail, foreground_of,
                    guard_terminated, log_auto, rel, screen_view,
                    texts_payload)
from multi_stage_swipe import check_bounds, run_swipe
from runtime import execution, timed_call

_SYSTEM_PACKAGES = {
    "android", "com.android.systemui", "com.zui.launcher",
    "com.android.launcher", "com.android.launcher3",
    "com.google.android.apps.nexuslauncher",
}
_HOME_COMMAND = (
    "cmd package resolve-activity --brief --user current "
    "-a android.intent.action.MAIN -c android.intent.category.HOME"
)


def _shell_text(d, command: str) -> str:
    response = d.shell(command)
    if getattr(response, "exit_code", 0) != 0:
        raise ValueError("系统状态查询失败")
    text = getattr(response, "output", response)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("系统状态查询没有返回内容")
    return text


def _require_foreground_app(d, foreground: dict) -> None:
    package, activity = foreground.get("package"), foreground.get("activity")
    if not package or not activity:
        raise ValueError("无法确认前台应用，拒绝执行该手势")
    if (package in _SYSTEM_PACKAGES
            or activity.rsplit(".", 1)[-1].lower() in
            {"recentsactivity", "recents", "overviewactivity", "resolveractivity"}):
        raise ValueError(f"前台为桌面、最近任务或系统界面（{package}/{activity}），拒绝执行该手势")

    # 动态识别第三方桌面；解析失败时不能仅凭包名猜测它是普通应用。
    resolved = _shell_text(d, _HOME_COMMAND)
    home = re.search(r"^\s*([\w.]+)/[\w.$]+\s*$", resolved, re.MULTILINE)
    if not home or home.group(1) == "android":
        raise ValueError("无法可靠识别当前桌面，拒绝执行该手势")
    home_package = home.group(1)
    if package == home_package:
        raise ValueError(f"前台为桌面（{package}），拒绝执行该手势")

    # 通知栏盖住 App 时 resumed Activity 仍可能是 App，要再查实际焦点。
    windows = _shell_text(d, "dumpsys window")
    match = re.search(r"^\s*mCurrentFocus=(.+)$", windows, re.MULTILINE)
    focus = match.group(1) if match else ""
    if not focus or focus.strip() == "null":
        raise ValueError("无法确认当前焦点窗口，拒绝执行该手势")
    if any(marker in focus.lower() for marker in
           ("notificationshade", "statusbar", "keyguard", "lockscreen")):
        raise ValueError("当前焦点为系统面板或锁屏，拒绝执行该手势")
    focused = re.search(r"([\w.]+)/[\w.$]+", focus)
    if not focused or focused.group(1) != package:
        raise ValueError("前台应用与焦点窗口不一致，拒绝执行该手势")


def _gesture_record(action, label, start, end, *, direction, duration_ms, hold_ms) -> dict:
    """整理已计算的动作取证数据；不保存预设配置，也不注入触摸。"""
    return {
        "type": f"gesture:{action}", "label": label, "direction": direction,
        "from": list(start), "to": list(end),
        "distance_px": abs(end[0] - start[0]) + abs(end[1] - start[1]),
        "dur_ms": duration_ms, "hold_ms": hold_ms,
    }


def show_taskbar(width: int, height: int) -> dict:
    """底部中央上划屏高的 10%，移动 300ms 后抬手。"""
    percent = 0.1
    start = (width // 2, height - 1)
    end = (start[0], start[1] - round(height * percent))
    return _gesture_record(
        "show_taskbar", "上划唤起任务栏", start, end, direction="up",
        duration_ms=300, hold_ms=0,
    )


def recents(width: int, height: int) -> dict:
    """底部中央上划屏高的 40%，移动 600ms，保持按住 500ms 后抬手。"""
    percent = 0.4
    start = (width // 2, height - 1)
    end = (start[0], start[1] - round(height * percent))
    return _gesture_record(
        "recents", "上划停留进入最近任务", start, end, direction="up",
        duration_ms=600, hold_ms=500,
    )


def home(width: int, height: int) -> dict:
    """底部中央上划屏高的 40%，移动 600ms 后抬手。"""
    percent = 0.4
    start = (width // 2, height - 1)
    end = (start[0], start[1] - round(height * percent))
    return _gesture_record(
        "home", "上划进入桌面", start, end, direction="up",
        duration_ms=600, hold_ms=0,
    )


def switch_app_left(width: int, height: int) -> dict:
    """在 H-10 处从 75% 屏宽左滑 50% 屏宽，移动 500ms 后抬手。"""
    start_ratio = 0.75
    end_ratio = 0.25
    start = (round(width * start_ratio), height - 10)
    end = (round(width * end_ratio), start[1])
    return _gesture_record(
        "switch_app_left", "底部左滑切换相邻应用", start, end, direction="left",
        duration_ms=500, hold_ms=0,
    )


def switch_app_right(width: int, height: int) -> dict:
    """在 H-10 处从 25% 屏宽右滑 50% 屏宽，移动 500ms 后抬手。"""
    start_ratio = 0.25
    end_ratio = 0.75
    start = (round(width * start_ratio), height - 10)
    end = (round(width * end_ratio), start[1])
    return _gesture_record(
        "switch_app_right", "底部右滑切换相邻应用", start, end, direction="right",
        duration_ms=500, hold_ms=0,
    )


# 只分发动作名称；轨迹和时长分别写在上面的五个动作方法内。
_ACTION_HANDLERS = {
    "show_taskbar": show_taskbar,
    "recents": recents,
    "home": home,
    "switch_app_left": switch_app_left,
    "switch_app_right": switch_app_right,
}


def prepare_gesture(d, action: str) -> dict:
    """只读检查全部前置条件、计算轨迹；不注入触摸。"""
    handler = _ACTION_HANDLERS[action]
    width, height = d.window_size()
    if (type(width) is not int or type(height) is not int or width <= 0 or height <= 0):
        raise ValueError("无法获得有效的当前屏幕尺寸")
    spec = handler(width, height)
    if spec["distance_px"] <= 0:
        raise ValueError("屏幕尺寸过小，无法执行预设手势")
    check_bounds(spec["from"], [{"type": "move", "to": spec["to"]}], (width, height))
    foreground = foreground_of(d)
    if action in {"show_taskbar", "switch_app_left", "switch_app_right"}:
        _require_foreground_app(d, foreground)
    spec["screen_size"] = [width, height]
    spec["foreground_before"] = foreground
    return spec


def execute_gesture(d, spec: dict) -> None:
    start, end = tuple(spec["from"]), tuple(spec["to"])
    if spec["hold_ms"]:
        run_swipe(d, start, [
            {"type": "move", "to": end, "dur": spec["dur_ms"]},
            {"type": "hold", "dur": spec["hold_ms"]},
        ])
    else:
        result = d.swipe(*start, *end, duration=spec["dur_ms"] / 1000)
        if result is False:
            raise RuntimeError("设备报告滑动注入失败")


class GestureArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        fail(f"gesture 参数错误: {message}", rejected=True)


def main() -> None:
    ensure_utf8_stdio()
    execution("not_executed")
    parser = GestureArgumentParser(description="gesture：底部上划导航、底部左右滑动切换相邻应用的固定预设")
    parser.add_argument("action", choices=tuple(_ACTION_HANDLERS))
    parser.add_argument("--why", default=None, help="动作理由，记录到时间轴")
    add_common_args(parser)
    apply_stdin_args()
    args = parser.parse_args()
    if args.settle < 0:
        fail("settle 必须为非负整数毫秒", rejected=True)
    if args.perm_action is not None or args.perm is not None:
        fail("gesture 不接受权限处理参数", rejected=True)
    guard_terminated(f"gesture {args.action}")
    try:
        device = connect(args.serial)
    except Exception as error:
        fail(f"设备连接失败: {error}")

    try:
        spec = prepare_gesture(device, args.action)
        if args.why:
            spec["why"] = args.why
        png, xml, meta = evidence_paths(args.dir, args.name)
    except Exception as error:
        fail(f"手势准备失败，未执行: {error}", rejected=True)

    started_at = datetime.now().isoformat(timespec="milliseconds")
    t0 = time.perf_counter()
    execution("may_have_executed")
    try:
        timed_call("action", execute_gesture, device, spec)
    except Exception as error:
        fail(f"手势执行失败，不要自动重试: {error}")
    execution("executed")

    try:
        timed_call("settle_wait", time.sleep, args.settle / 1000)
        info = capture(device, png, xml, meta, extra={"action": spec})
        xml_text = xml.read_text(encoding="utf-8")
        # 系统手势可能进入 Launcher 或切换应用；所有预设均返回全屏节点。
        view = screen_view(xml_text, mode="full" if args.full else "nav",
                           fg_package=None)
        info["sig"] = view["sig"]
        ck = checkpoint.evaluate(checkpoint.parse_expect(args.expect),
                                 activity=info.get("activity"), sig=view["sig"])
        if ck["checked"]:
            info["checkpoint"] = ck
    except Exception as error:
        fail(f"手势已执行但取证失败，不要重试手势: {error}")

    detail = (f"{spec['label']} · 移动 {spec['dur_ms']}ms"
              f" · 终点停留 {spec['hold_ms']}ms")
    if ck["checked"]:
        detail += " · 检查点命中" if ck["ok"] else " · 检查点未命中"
    info["duration_scope"] = (
        "gesture through capture/view/checkpoint; excludes preflight, event write and response")
    duration_ms = round((time.perf_counter() - t0) * 1000)
    log_auto("act", f"gesture {args.action}", detail, info, rel(png),
             duration_ms, started_at)

    payload = {
        **info,
        "evidence": {"png": str(png), "xml": str(xml), "meta": str(meta)},
        "applied": None,
        "applied_note": "手势已执行；没有通用可靠标记可自动确认目标界面。",
        "next": view["next"], "nodes": view["nodes"],
        "nodes_count": len(view["nodes"]),
        "nodes_mode": "full" if args.full else "nav",
        **texts_payload(view["texts"]),
        "hint": ("请根据本次截图确认目标界面，不要因 applied=null 重试。"
                 "next 包含应用、任务栏及系统界面目标。"),
    }
    if ck["checked"] and not ck["ok"]:
        payload["checkpoint_hint"] = "检查点未命中: " + "; ".join(ck["missed"])
    payload.pop("sig", None)
    payload.pop("timing", None)
    emit(True, payload)


if __name__ == "__main__":
    main()
