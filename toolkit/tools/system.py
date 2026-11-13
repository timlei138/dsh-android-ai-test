"""system：系统级界面动作（通知栏 / 快捷设置 / 亮屏解锁 / 横竖屏切换）。

为什么单独一个工具，而不是让 AI 用 swipe 手势：
  ① **手势不可靠**。下拉通知栏要求起点贴着屏幕最顶端、速度有讲究、
     坐标还随分辨率变。历史数据里同一会话内重复拉通知栏的有 9 个，
     why 写着「再试一次拉下通知栏」——重试本身就是浪费。
     系统命令 `cmd statusbar expand-notifications` 只有 91~137ms，
     u2 的 `openNotification` jsonrpc 更是只要 **43ms**（实测中位）。
  ② **少一次往返**。一次 android_* 工具调用固定要花 4.8~6.4 秒
     （起 shell + 起 python + u2.connect + settle + 截图 + dump）。
     「act 下拉 → observe 看」是两次调用 ≈ 9.6 秒；
     本工具一次调用内做完「打开 → 截图 → dump → 解析成通知列表」。
  ③ **结果结构化**。直接把通知卡片解析成
     `notifications: [{title, texts, clickable, center}]`，
     AI 不必自己从 dump 里猜哪几个 text 是同一条通知。

动作（positional）：
  notifications    展开通知栏，返回通知卡片列表（默认保持展开）
  quick_settings   展开快捷设置面板（默认保持展开）
  close_shade      收起通知栏 / 快捷设置面板
  wake             点亮屏幕（不改变当前 App）
  unlock           点亮并尝试上滑解锁（无密码锁屏）
  rotate           切换横竖屏，返回切换前后的宽高（--to portrait|landscape|toggle）

用法：
  python system.py notifications --why "判提醒有没有来" --name case85/step07
  python system.py notifications --close --name ...      # 看一眼就收起
  python system.py close_shade
  python system.py wake

和 act.py 的关系（共用同一套记录通道，不是另一条路）：
  · 证据同样是「一张截图 + 一份 dump + 一份 meta」，落在同一个 step 目录，
    回放/时间线/统计口径完全一致；
  · 事件 kind 记 `act`（它确实是一个动作），`tool` 写 `system <动作名>`，
    所以既有按 act 统计的口径不会把这个当"看"；
  · 支持 `--expect` 检查点，判定由工具做，不需要 AI 再读 dump。

刻意不做的事：
  · **不处理权限弹窗**。权限框只会被「点被测 App 里的控件」触发，
    系统级动作点不到那儿（用户 2026-11-06 裁决：只有 tap/longclick 查权限）。
  · **不做崩溃检测**。崩溃检测按前台包查，而打开通知栏后前台是
    `com.android.systemui` —— 拿 systemui 去查崩溃只会是噪音。
    崩溃由 act / observe 兜底。
  · **不回填会话包名**。同一个理由：此刻前台是 systemui，不是被测 App。
"""
from __future__ import annotations

import argparse
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from common import (add_common_args, apply_stdin_args, capture, connect, emit,
                    evidence_paths, fail, guard_terminated, labeled_nodes,
                    log_auto, next_targets, npm_identity, rel, summarize_xml)
from runtime import execution, timed_call
import checkpoint


# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ⚠️/✅ 就崩。
#   必须在任何输出之前调用（含 argparse 的报错输出）。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass


ACTIONS = ("notifications", "quick_settings", "close_shade", "wake", "unlock", "rotate")

# 通知栏列表容器 / 单条通知行 / 快捷设置面板容器的 resource-id 片段
# （AOSP 通用名，不是某个 App 的）
SCROLLER_ID = "notification_stack_scroller"
NOTIF_ROW_ID = "expandableNotificationRow"
QS_PANEL_ID = "quick_settings_container"

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

LABELS = {
    "notifications": "展开通知栏",
    "quick_settings": "展开快捷设置",
    "close_shade": "收起通知栏",
    "wake": "点亮屏幕",
    "unlock": "点亮并解锁",
    "rotate": "切换横竖屏",
}


def _center(bounds: str | None) -> list[int] | None:
    m = _BOUNDS_RE.search(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    return [(x1 + x2) // 2, (y1 + y2) // 2]


def _rid_of(node) -> str:
    return (node.get("resource-id") or "").split("/")[-1]


def _parse_shade(xml_text: str) -> dict:
    """从 dump 里解出通知卡片列表。

    返回 dict：
      container_found —— 找到通知列表容器（`notification_stack_scroller`）了没有
      rows_detected   —— 认出「通知行」（`expandableNotificationRow`）了没有
      notifications   —— 一条条真通知（AI 判「提醒有没有来」只看这个）
      controls        —— 同容器里的其余行（"静音"、"历史记录/管理" 这类面板按钮）
      note            —— 认不出来时给 AI 的说明（认出来时是空串）

    ⚠️ **认不出来要如实说，不能返回空列表了事。**
    `rows_detected=False` 时 `notifications` 一定是空的，但那时它的含义
    不是「没有通知」，而是「这个 ROM 的通知行 id 认不出来」。这两件事对
    用例判定的影响完全相反 —— 混在一起会让 AI 把「没看清」当成「没提醒
    来」，直接判 FAIL。所以宁可多给一个 `note`，也不让它自由心证。
    """
    out = {"container_found": False, "rows_detected": False,
           "notifications": [], "controls": [], "note": ""}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        out["note"] = ("dump 解析失败（XML 不完整），这次拿不到通知内容，"
                       "请看截图判断。")
        return out

    scroller = None
    for node in root.iter():
        if SCROLLER_ID in (node.get("resource-id") or ""):
            scroller = node
            break
    if scroller is None:
        out["note"] = ("dump 里没有通知列表容器（notification_stack_scroller），"
                       "所以没解析通知 —— 可能是面板没真正展开，也可能是这个 "
                       "ROM 换了容器名。请看截图判断。")
        return out

    out["container_found"] = True
    has_rows = False
    for row in list(scroller):
        texts: list[str] = []
        for sub in row.iter():
            t = (sub.get("text") or "").strip()
            if t and t not in texts:
                texts.append(t)
        if not texts:
            continue
        is_note = (NOTIF_ROW_ID in (row.get("resource-id") or "")
                   or any(NOTIF_ROW_ID in (s.get("resource-id") or "")
                          for s in row.iter()))
        item = {
            "title": texts[0][:120],
            "texts": texts[:8],
            "clickable": (row.get("clickable") == "true"
                          or any(s.get("clickable") == "true" for s in row.iter())),
            "bounds": row.get("bounds"),
            "center": _center(row.get("bounds")),
            "rid": _rid_of(row),
        }
        if is_note:
            has_rows = True
            out["notifications"].append(item)
        else:
            out["controls"].append(item)

    out["rows_detected"] = has_rows
    if not has_rows:
        out["note"] = ("找到了通知列表容器，但一条通知行都没认出来"
                       "（expandableNotificationRow）。**这不等于「没有通知」** ——"
                       "更可能是这个 ROM/主题换了行 id。请直接看截图判断，"
                       "不要据此判「没有提醒」。")
    return out


def _shade_open(xml_text: str, activity: str | None) -> bool:
    """通知栏 / 快捷设置面板是否展开。

    判据用**物理标记**：dump 里有没有通知列表容器（`notification_stack_scroller`）
    或快捷设置容器（`quick_settings_container`），或前台 activity 是不是
    `NotificationShade`。

    ⚠️ 两个容器必须都认：只认通知列表的话，`quick_settings` 展开后
    `notification_stack_scroller` 根本不出现（实测 dump 里只有 qs_* 那一串），
    会被误判成「没展开」。也不能用「有 text 就算展开」——普通页面也有一堆 text。
    """
    return (SCROLLER_ID in xml_text or QS_PANEL_ID in xml_text
            or "NotificationShade" in (activity or ""))


def _shade_kind(xml_text: str) -> str | None:
    """展开的是哪块面板：'quick_settings' / 'notifications' / None（没展开）。

    `quick_settings` 优先：快捷设置展开时 dump 里**只有** qs_* 那一串，
    不会同时出现通知列表容器，所以先判它不会误伤。
    """
    if QS_PANEL_ID in xml_text:
        return "quick_settings"
    if SCROLLER_ID in xml_text:
        return "notifications"
    return None


def _applied(args_action: str, *, opened: bool, kind: str | None,
             xml_text: str, d=None) -> tuple[bool | None, str]:
    """这个动作**证实**生效了吗？返回 (True/False/None, 说明)。

    三态，不是布尔 —— 这是刻意的：
      True  —— 有可靠物理证据证明生效
      False —— 有可靠物理证据证明没生效
      None  —— **判断不了**，此时绝不硬判（硬判 false 会让 AI 重试；
               硬判 true 会让它把「没看清」当成「成功了」）

    `rotate` 判得了：切换后回读 `window_size`，宽高真的换向了才算生效。
    判不了的情况（拿不到尺寸）返回 None，由 AI 看截图确认 ——
    工具只负责说「我判不了」，不硬判。
    """
    if args_action in ("notifications", "quick_settings"):
        want = "quick_settings" if args_action == "quick_settings" else "notifications"
        if opened and kind == want:
            return True, ""
        if opened and kind and kind != want:
            got = "快捷设置" if kind == "quick_settings" else "通知栏"
            need = "快捷设置" if want == "quick_settings" else "通知栏"
            return False, f"打开后展开的是{got}，不是你要的{need}。"
        if opened:
            return True, "面板已展开（认出 NotificationShade 焦点，但容器名不认识）。"
        return False, ""

    if args_action == "close_shade":
        if not opened:
            return True, ""
        return False, "收起后 dump 里仍有面板容器。"

    if args_action == "wake":
        on = _screen_on(d) if d is not None else None
        if on is None:
            return None, "拿不到亮屏状态（d.info 与 dumpsys power 都失败），请看截图。"
        if on:
            return True, ""
        return False, "wakeUp 之后屏幕仍然是灭的。"

    if args_action == "unlock":
        on = _screen_on(d) if d is not None else None
        if on is None:
            return None, "拿不到亮屏状态，请看截图确认有没有到桌面。"
        if not on:
            return False, "解锁之后屏幕仍然是灭的。"
        if "keyguard" in xml_text.lower() or "lockscreen" in xml_text.lower():
            return False, "屏幕亮了但还在锁屏上（dump 里有 keyguard）—— 可能设了密码/图案。"
        return True, ""

    if args_action == "rotate":
        size = _window_size(d) if d is not None else None
        if not size:
            return None, "拿不到尺寸（window_size 失败），请看截图确认朝向。"
        return True, ""

    # 兜底：未知动作一律不硬判
    return None, "这个动作没有可自动判定的物理标记，请看截图确认。已执行，动作本身没报错。"


def _screen_on(d) -> bool | None:
    """屏幕亮着吗？None = 判断不了。

    ⚠️ 不能只用 `d.info["screenOn"]`：实测这台 Android 17 上 `d.info` 直接抛
    `RPCUnknownError: java.lang.IllegalStateException: ApplicationSharedMemory
    not initialized`（uiautomator 的 apk 在某些 ROM 上取 DisplaySizeDp 会崩），
    连亮屏状态都拿不到。所以先走 jsonrpc，失败退 `dumpsys power` 解析。
    """
    try:
        info = d.info or {}
        if "screenOn" in info:
            return bool(info["screenOn"])
    except Exception:  # noqa: BLE001  jsonrpc 在部分 ROM 上不可用
        pass
    try:
        out = (d.shell("dumpsys power | grep -m1 'mWakefulness='").output or "")
    except Exception:  # noqa: BLE001
        return None
    if "mWakefulness=" not in out:
        return None
    # Awake / Dozing / Asleep / Dreaming / Shutdown
    return "mWakefulness=Awake" in out or "mWakefulness=Dreaming" in out


def _ensure_awake(d) -> bool:
    """屏幕灭着就先点亮。返回「原本就是亮的吗」。

    ⚠️ 实测踩过：睡眠态下所有 dump 长度恒定、`mCurrentFocus` 恒为
    `NotificationShade`，一切测量都是假的。动作之前必须先把屏点亮。
    """
    on = _screen_on(d)
    if on is None or on:
        return True
    try:
        d.jsonrpc.wakeUp()
        time.sleep(0.7)
    except Exception:  # noqa: BLE001
        pass
    return False


_ROT_LABEL = {0: "portrait", 1: "landscape"}


def _window_size(d) -> tuple[int, int] | None:
    """当前逻辑分辨率 (宽, 高)。拿不到返回 None —— 不编造尺寸。

    ⚠️ 这台机（Android 17 / ZUXOS）上 `d.rotation` 直接 AttributeError，
    `d.info` 会抛 ApplicationSharedMemory —— 只有 `d.window_size()` 可靠。
    """
    try:
        w, h = d.window_size()
        if w and h:
            return int(w), int(h)
    except Exception:  # noqa: BLE001
        pass
    return None


def _orientation_of(size: tuple[int, int] | None) -> str | None:
    if not size:
        return None
    return "landscape" if size[0] > size[1] else "portrait"


def _rotate(d, to: str | None) -> tuple[dict, dict]:
    """切横竖屏，返回 (切换前, 切换后) 两份尺寸快照。

    `to` = portrait / landscape / toggle（默认 toggle）。

    ⚠️ 实测（Android 17 / ZUXOS 3.0，3040x1904）踩过两个坑：
      · `settings put system user_rotation N` **只写设置、不当场转** —— 要等
        下一次旋转事件才按新值转，so 光写 settings 加个 sleep 不可靠。
      · `cmd window set-user-rotation locked N` **这个子命令不存在**：
        静默失败、不报错、也不转屏（曾经因此把「上一步 settings 延迟生效」
        误判成「这条命令生效了」，方向都判反）。
      对的是 `cmd window user-rotation lock <0|1>` / `free`，实测立刻转向：
        lock 0 → 竖屏，lock 1 → 横屏，free → 跟随传感器。
      它本身就是锁定，**不必再改 accelerometer_rotation**（改了还得还原，
      反而多一处能忘的副作用）。
    """
    before = _window_size(d)
    cur = _orientation_of(before)
    target = (to or "toggle").lower()
    if target == "toggle":
        target = "landscape" if cur == "portrait" else "portrait"
    if target not in ("portrait", "landscape"):
        raise ValueError(f"--to 只能是 portrait/landscape/toggle，收到 {to!r}")
    angle = 0 if target == "portrait" else 1

    d.shell(f"cmd window user-rotation lock {angle}")

    # 轮询回读（最多 3 秒）—— 转屏是异步的，一次读很可能还是旧值
    after = before
    for _ in range(12):
        time.sleep(0.25)
        after = _window_size(d)
        if after != before and _orientation_of(after) == target:
            break

    snap_b = {"width": before[0] if before else None,
              "height": before[1] if before else None,
              "orientation": cur}
    snap_a = {"width": after[0] if after else None,
              "height": after[1] if after else None,
              "orientation": _orientation_of(after),
              "requested": target,
              "changed": (after != before)}
    return snap_b, snap_a


def _close_shade(d) -> None:
    """收起通知栏 / 快捷设置。

    ⚠️ 不能用「再 openNotification 一次」来收起 —— 实测连调两次仍是展开
    （`NotificationShade` 还在）。能收起的三条：back 键、`cmd statusbar collapse`、
    从下往上滑。这里用 back（最快，且不会像 home 那样把 App 切到后台）。
    """
    d.press("back")
    time.sleep(0.2)
    out = (d.shell("dumpsys window | grep -m1 mCurrentFocus").output or "")
    if "NotificationShade" in out:
        # back 没关掉（个别 ROM 上通知栏抢焦点）→ 退到系统命令
        d.shell("cmd statusbar collapse")
        time.sleep(0.3)


def _unlock(d) -> None:
    """点亮并上滑解锁（针对无密码锁屏）。

    不用 u2 的 `d.unlock()`：它是「先 power 再滑」——屏幕已经亮着时
    那个 power 会把屏幕**关掉**，反而更糟。这里只在灭屏时点亮，然后上滑。
    """
    _ensure_awake(d)
    try:
        w, h = d.window_size()
    except Exception:  # noqa: BLE001
        w, h = 1080, 1920
    d.swipe(int(w * 0.5), int(h * 0.85), int(w * 0.5), int(h * 0.25), 0.15)
    time.sleep(0.5)


def main() -> None:
    p = argparse.ArgumentParser(description="system：系统级界面动作")
    p.add_argument("action", choices=ACTIONS, help="要做的系统动作")
    p.add_argument("--close", action="store_true",
                   help="notifications/quick_settings 用：证据拍完顺手收起。"
                        "证据仍是「展开的样子」；收起后 next 为空（那些坐标已失效）")
    p.add_argument("--to", choices=("portrait", "landscape", "toggle"), default="toggle",
                   help="rotate 用：切到竖屏 / 横屏 / 切到当前朝向的反面（默认 toggle）")
    p.add_argument("--why", default=None, help="这一步的理由（入库展示在时间轴）")
    # --settle / --expect / --full / --no-filter / --name 等（与 act 同一套）
    add_common_args(p)
    apply_stdin_args()   # 支持 --stdin-json（参数走 stdin，避开 Windows 引号问题）
    args = p.parse_args()

    guard_terminated(f"system {args.action}")   # 会话已因崩溃终止 → 拒绝执行

    try:
        d = connect(args.serial)
    except Exception as e:  # noqa: BLE001
        fail(f"设备连接失败: {e}")

    png, xml, meta = evidence_paths(args.dir, args.name)
    t0 = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="milliseconds")
    execution("may_have_executed")

    was_awake = True
    rot_before = rot_after = None
    try:
        if args.action in ("notifications", "quick_settings", "wake", "unlock", "rotate"):
            was_awake = _ensure_awake(d)
        if args.action == "notifications":
            timed_call("action", d.jsonrpc.openNotification)
        elif args.action == "quick_settings":
            timed_call("action", d.jsonrpc.openQuickSettings)
        elif args.action == "close_shade":
            timed_call("action", _close_shade, d)
        elif args.action == "wake":
            timed_call("action", d.jsonrpc.wakeUp)
        elif args.action == "unlock":
            timed_call("action", _unlock, d)
        else:  # rotate
            rot_before, rot_after = _rotate(d, args.to)
    except Exception as e:  # noqa: BLE001
        fail(f"动作执行失败: {e}")

    execution("executed")
    # 展开面板有滑入动画，等 settle 再 dump —— 和 act 一样是硬性的（默认 500ms）。
    # rotate 已经在 _rotate() 里轮询过尺寸了，这里照常等 settle 拍证据。
    timed_call("settle_wait", time.sleep, args.settle / 1000)

    detail = LABELS[args.action]
    if args.action == "rotate" and rot_after:
        b, a = rot_before or {}, rot_after
        detail += f" {b.get('width')}x{b.get('height')} → {a.get('width')}x{a.get('height')}"
        detail += "（已切）" if a.get("changed") else "（⚠️ 尺寸没变）"
    action = {"type": f"system:{args.action}"}
    if args.why:
        action["why"] = args.why

    try:
        info = capture(d, png, xml, meta, extra={"action": action})
    except Exception as e:  # noqa: BLE001
        fail(f"动作已执行但采集失败: {e} | action={args.action}")

    xml_text = xml.read_text(encoding="utf-8")
    activity = info.get("activity")

    # 面板状态 + 通知列表解析（这是本工具相对「act 下拉 + observe 看」的核心增益）
    # **只有 `notifications` 才解析通知**：
    #   · `quick_settings` 展开时 dump 里本来就没有通知列表容器，去解析只会
    #     得到一句「没找到容器」的噪音，干扰 AI 读结果；
    #   · wake/unlock/rotate/close_shade 的 dump 里也不该有面板。
    opened = _shade_open(xml_text, activity)
    kind = _shade_kind(xml_text)
    shade_actions = ("notifications", "quick_settings")
    if args.action == "notifications":
        shade = _parse_shade(xml_text)
    else:
        shade = {"container_found": False, "rows_detected": False,
                 "notifications": [], "controls": [], "note": ""}
    notifications = shade["notifications"]
    controls = shade["controls"]
    # 「动作生效了吗」由工具判 —— 但只判能证实的（见 _applied 的三态说明）
    applied, applied_note = _applied(
        args.action, opened=opened, kind=kind, xml_text=xml_text, d=d)
    if args.action in shade_actions:
        detail += "（已展开）" if opened else "（⚠️ 没展开）"
        if notifications:
            detail += f" · 通知 {len(notifications)} 条：" + " ｜ ".join(
                n["title"][:24] for n in notifications[:3])

    # 检查点：和 act 一致，由工具判「落在预期位置没有」，不阻断
    _sig = npm_identity(xml_text,
                        None if getattr(args, "no_filter", False) else None)
    info = {**info, "sig": _sig}
    _ck = checkpoint.evaluate(checkpoint.parse_expect(getattr(args, "expect", None)),
                              activity=activity, sig=_sig)
    if _ck["checked"]:
        info = {**info, "checkpoint": _ck}
        detail += (" · ✅检查点命中" if _ck["ok"]
                   else " · ⚠️检查点未命中: " + "; ".join(_ck["missed"]))

    # --close：拍完再收，这样证据里还是"展开的样子"（用例要判的就是它）
    closed = False
    if args.close and args.action in ("notifications", "quick_settings"):
        try:
            _close_shade(d)
            closed = True
            detail += " · 已收起"
        except Exception:  # noqa: BLE001  收起失败不该抹掉已经拿到的证据
            detail += " · ⚠️收起失败"

    duration_ms = round((time.perf_counter() - t0) * 1000)
    info["duration_scope"] = ("core operation through capture/signature/checkpoint; "
                             "excludes connection, event write and response")
    if not was_awake:
        detail += " · 原屏幕是灭的，已先点亮"
    log_auto("act", f"system {args.action}", detail, info, rel(png),
             duration_ms, started_at)

    # ⚠️ 面板展开时**不能按前台包过滤**：通知栏/快捷设置是 `com.android.systemui`
    # 盖在 App 上画的，`foreground_of()` 读到的仍是被盖住的那个 App
    # （实测 `com.zui.calendar.AllInOneActivity`）。照它过滤会把面板里所有控件
    # 全滤掉（实测 nodes_count=0、next 空）。所以：面板开着就不过滤。
    fg = None if (getattr(args, "no_filter", False) or opened) else info.get("package")
    nodes = summarize_xml(xml_text,
                          mode="full" if getattr(args, "full", False) else "nav",
                          fg_package=fg)
    payload = {
        "action": action,
        **info,
        "evidence": {"png": str(png), "xml": str(xml), "meta": str(meta)},
        # 动作生效了吗：True / False / None（None = 判不了，看截图）
        "applied": applied,
        "applied_note": applied_note,
        "next": next_targets(nodes, labeled=labeled_nodes(xml_text, fg)),
        "nodes": nodes,
        "nodes_count": len(nodes),
        "nodes_mode": "full" if getattr(args, "full", False) else "nav",
        "hint": "",
    }
    if args.action == "rotate" and rot_after:
        payload["rotate"] = {
            "before": rot_before,
            "after": rot_after,
            "changed": rot_after.get("changed"),
            "requested": rot_after.get("requested"),
        }
    # 面板相关字段只在「展开面板」的两个动作上出现 —— 其它动作没有面板可谈，
    # 塞一堆空字段和「没找到容器」的说明只会干扰 AI 读结果。
    if args.action in shade_actions:
        payload.update({
            "shade_open": opened,
            "shade_kind": kind,
            "notifications": notifications,
            "notification_count": len(notifications),
            # 认不认得出「通知行」——False 时上面的空 list **不代表没有通知**
            "rows_detected": shade["rows_detected"],
            "shade_container_found": shade["container_found"],
            "shade_note": shade["note"],
            "shade_controls": controls,
            "closed_after_capture": closed,
        })
    # sig / timing 只落盘、不回传（同 act.py 的理由）
    payload.pop("sig", None)
    payload.pop("timing", None)

    if _ck["checked"]:
        payload["checkpoint"] = _ck
        if not _ck["ok"]:
            payload["checkpoint_hint"] = (
                "⚠️ 落点与预期不符：未命中 " + "; ".join(_ck["missed"]) + "。"
                "先判断是「多余的临时弹窗」（不影响主流程 → 关掉继续）还是"
                "「用例要的东西不存在了」（主流程被改 → 停止并如实记录）。")

    # 给 AI 的下一步提示：按「动作生效了吗」三态分派，把「现在什么状态、接下来怎么走」
    # 说清，免得它再去 act 里一步步试探（那正是这个工具要省掉的往返）。
    if applied is None:
        # 判不了 —— 动作没报错，但工具不敢替它下结论
        payload["hint"] = (
            f"{LABELS[args.action]}：{applied_note}"
            "动作本身没报错。**请用这次返回的截图确认它到底生效了没有**，"
            "别重试 —— 重试不会让判断更清楚。")
    elif not applied:
        payload["hint"] = (
            f"⚠️ {LABELS[args.action]}**没有生效**。"
            + (applied_note or "")
            + "可能屏幕灭着、有锁屏挡着，或者这个 ROM 不认这个调法。"
            "先 android_system(action='unlock') 再试一次；**同一步最多试 2 次**，"
            "还不通就如实记下来往下走，不要反复重试。")
    elif args.action == "notifications":
        if not shade["rows_detected"]:
            payload["hint"] = (
                "通知栏已展开，但" + shade["note"] + "面板里认出的其他控件在 "
                "`shade_controls` 和 `next` 里。")
        elif notifications:
            payload["hint"] = (
                f"通知栏已展开，解析出 {len(notifications)} 条通知"
                "（见 `notifications`：title/texts/clickable/center）。"
                "要判「提醒有没有来」直接看这个列表，**不必再 observe**。"
                "要打开某条通知就从 `notifications[].center` 取坐标点它；"
                "不需要了就调 android_system(action='close_shade') 收起 ——"
                "**展开的通知栏会挡住后续操作**。")
        else:
            payload["hint"] = (
                "通知栏已展开，通知列表是空的 —— 这次确实没有通知。"
                "如果用例就是判「提醒有没有来」，这就是「没来」的证据；"
                "**但先看截图确认一眼**再下结论。用完记得 "
                "android_system(action='close_shade') 收起。")
    elif args.action == "quick_settings":
        payload["hint"] = (
            "快捷设置面板已展开，面板里的开关在 `next` 里（t=文字，c=坐标）。"
            "它不是判通知用的，别在这儿找通知。"
            "不需要了就调 android_system(action='close_shade') 收起。")
    elif args.action == "close_shade" or closed:
        payload["hint"] = "面板已收起，现在看到的就是收起后的画面。"
    elif args.action == "wake":
        payload["hint"] = "屏幕已点亮（没动当前 App）。下一步正常操作即可。"
    elif args.action == "unlock":
        payload["hint"] = "已点亮并上滑解锁。下一步正常操作即可。"
    else:
        payload["hint"] = ("下一步点 `next` 里的目标（c=坐标，t=文字，r=rid）；"
                           "需要按页面文字断言时看 nodes（full=true 才是全量）。")

    emit(True, payload)


if __name__ == "__main__":
    main()
