import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import perm as P  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ── 夹具：拼一份和真机同构的权限框 dump ───────────────────────────────────
# 真机 rid 由 permissioncontroller 渲染（detect_from_xml 的物理标记）；
# 按钮 bounds 取自 #1000232 的真实 dump：三行互不重叠。
def _btn(rid, text, x1, y1, x2, y2, clickable=True):
    return (
        f'<node index="0" text="{text}" resource-id="'
        f'com.android.permissioncontroller:id/{rid}" class="android.widget.Button" '
        f'package="com.android.permissioncontroller" clickable="{str(clickable).lower()}" '
        f'bounds="[{x1},{y1}][{x2},{y2}]" />'
    )


def dialog_xml(prompt="日历正在尝试打开摄像头", deny="拒绝", with_plain_deny=True):
    """一份权限框 dump。with_plain_deny=False 模拟「只有拒绝并不再询问」的形态。"""
    parts = [
        '<hierarchy rotation="1">',
        '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
        'package="com.android.permissioncontroller" clickable="false" '
        'bounds="[0,0][3040,1904]">',
        f'<node index="0" text="{prompt}" resource-id="" class="android.widget.TextView" '
        'package="com.android.permissioncontroller" clickable="false" '
        'bounds="[970,616][2070,858]" />',
    ]
    if with_plain_deny:
        parts.append(_btn("permission_allow_foreground_only_button",
                          "仅在使用时允许", 970, 861, 2070, 1026))
        parts.append(_btn("permission_allow_one_time_button",
                          "仅本次使用时允许", 970, 1029, 2070, 1194))
        parts.append(_btn("permission_deny_button", deny, 970, 1197, 2070, 1362))
    else:
        parts.append(_btn("permission_allow_foreground_only_button",
                          "仅在使用时允许", 970, 861, 2070, 1026))
        parts.append(_btn("permission_allow_one_time_button",
                          "仅本次使用时允许", 970, 1029, 2070, 1194))
        parts.append(_btn("permission_deny_and_dont_ask_again_button",
                          "拒绝并不再询问", 970, 1197, 2070, 1362))
    parts.append("</node></hierarchy>")
    return "".join(parts)


class FakeDevice:
    """只记 click 的假设备。"""

    def __init__(self):
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((x, y))


def run(frames, action=None, denied=None, recorded=None):
    """frames: 依次返回的 dump 列表（None 表示画面里没有权限框）。"""
    frames = list(frames)
    dev = FakeDevice()
    seen = []

    def shot():
        xml = frames.pop(0) if frames else None
        seen.append(xml)
        return (xml or "<hierarchy></hierarchy>", {})

    def on_denied(key):
        if recorded is not None:
            recorded.append(key)

    result = P.handle(dev, action, shot=shot, on_dialog=None, settle_s=0,
                      denied_keys=set(denied or ()), on_denied=on_denied)
    return result, dev


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── dialog_key / is_media_dialog ─────────────────────────────────────────
@case("dialog_key 用申请文案当身份键（同一权限两次弹框键相同）")
def _():
    a = P.detect_from_xml(dialog_xml("日历正在尝试打开摄像头"))
    b = P.detect_from_xml(dialog_xml("日历正在尝试打开摄像头"))
    expect(P.dialog_key(a) == P.dialog_key(b), "同一文案两次弹框应得到同一个键")
    expect(P.dialog_key(a) == "日历正在尝试打开摄像头", "键就是申请文案本身")


@case("dialog_key 不同的申请文案得到不同键")
def _():
    a = P.detect_from_xml(dialog_xml("日历正在尝试打开摄像头"))
    b = P.detect_from_xml(dialog_xml("日历正在尝试访问您设备上的照片"))
    expect(P.dialog_key(a) != P.dialog_key(b), "摄像头与照片不是同一类权限")


@case("dialog_key 无文案时退回按钮集合，且不为空")
def _():
    xml = dialog_xml(prompt="")
    info = P.detect_from_xml(xml)
    key = P.dialog_key(info)
    expect(key != "", "文案取不到时也要有稳定指纹")
    expect("仅在使用时允许" in key, "指纹应含按钮文案，实际 " + key)


@case("dialog_key 对 None 返回空串")
def _():
    expect(P.dialog_key(None) == "", "None 应返回空串")


@case("is_media_dialog：摄像头/照片/视频/图片 都算媒体类")
def _():
    for prompt in ("日历正在尝试打开摄像头",
                   "日历正在尝试访问您设备上的照片",
                   "日历正在尝试录制视频",
                   "日历正在尝试访问图片"):
        info = {"prompt": prompt}
        expect(P.is_media_dialog(info), f"{prompt} 应判为媒体类")


@case("is_media_dialog：通知/日历/联系人/设备信息 不算媒体类")
def _():
    for prompt in ("日历正在尝试显示通知",
                   "日历正在尝试读取日历",
                   "日历正在尝试读取联系人",
                   "日历正在尝试获取设备信息",
                   "日历正在尝试写/删日历"):
        expect(not P.is_media_dialog({"prompt": prompt}), f"{prompt} 不该判为媒体类")


@case("is_media_dialog：拿不到文案一律 False（宁可不做两阶段）")
def _():
    expect(not P.is_media_dialog(None), "None 不该判为媒体类")
    expect(not P.is_media_dialog({}), "空 dict 不该判为媒体类")
    expect(not P.is_media_dialog({"prompt": ""}), "空文案不该判为媒体类")


# ── 两阶段核心 ─────────────────────────────────────────────────────────
@case("★媒体类第一次（未声明意图）→ 自动点「拒绝」")
def _():
    rec = []
    result, dev = run([dialog_xml(), None], recorded=rec)
    expect(result["detected"], "应检测到权限框")
    expect(result["handled"], "应点掉一个按钮")
    expect(result["clicks"][0]["text"] == "拒绝",
           f"第一次应点普通拒绝，实际 {result['clicks']}")
    expect(result.get("phase_first_deny") is True, "应标记 phase_first_deny")
    expect(result["action"] == "deny", f"action 应为 deny，实际 {result['action']}")
    expect(rec == ["日历正在尝试打开摄像头"],
           f"应回调 on_denied 把键持久化，实际 {rec}")


@case("★媒体类第二次（已拒过）→ 自动翻成「允许」")
def _():
    rec = []
    result, dev = run([dialog_xml(), None],
                      denied=["日历正在尝试打开摄像头"], recorded=rec)
    expect(result["clicks"][0]["text"] == "仅在使用时允许",
           f"第二次应点允许，实际 {result['clicks']}")
    expect(result.get("phase_advanced") is True, "应标记 phase_advanced")
    expect(result["action"] == "grant", "翻成 granted 后 action 应为 grant")
    expect(rec == [], "已经翻成允许时不该再回调 on_denied")


@case("★非媒体类权限（通知）→ 直接点「允许」，绝不自动拒绝")
def _():
    rec = []
    result, _dev = run([dialog_xml("日历正在尝试显示通知"), None], recorded=rec)
    expect(result["clicks"][0]["text"] == "仅在使用时允许",
           f"非媒体类应直接允许，实际 {result['clicks']}")
    expect(result["action"] == "grant", "非媒体类 action 应为 grant")
    expect(rec == [], "非媒体类不进两阶段")


@case("★只有「拒绝并不再询问」时 → 不点、unmatched，且**不记**已拒绝")
def _():
    rec = []
    result, dev = run([dialog_xml(with_plain_deny=False)], recorded=rec)
    expect(result.get("unmatched") is True, "应标记 unmatched")
    expect(dev.clicks == [], "绝不点「拒绝并不再询问」")
    expect(rec == [], "拒绝分支没验成，不能记「已拒绝」（否则下次会伪装成功）")
    expect("拒绝并不再询问" in (result.get("unmatched_reason") or ""),
           "应说明原因")


@case("★显式声明 deny：第一次照点拒绝")
def _():
    rec = []
    result, _dev = run([dialog_xml(), None], action="deny", recorded=rec)
    expect(result["clicks"][0]["text"] == "拒绝", f"实际 {result['clicks']}")
    expect(result["action_source"] == "declared", "来源应为 declared")
    expect(rec == ["日历正在尝试打开摄像头"], "声明 deny 也要记已拒绝")


@case("★显式声明 deny 但已拒过 → 仍然翻成允许（先拒后允才能走完）")
def _():
    result, _dev = run([dialog_xml(), None], action="deny",
                       denied=["日历正在尝试打开摄像头"])
    expect(result["clicks"][0]["text"] == "仅在使用时允许",
           f"声明 deny 第二次也应点允许，实际 {result['clicks']}")
    expect(result.get("phase_advanced") is True, "应标记 phase_advanced")


@case("显式声明 grant：媒体类第一次也直接允许")
def _():
    rec = []
    result, _dev = run([dialog_xml(), None], action="grant", recorded=rec)
    expect(result["clicks"][0]["text"] == "仅在使用时允许",
           f"实际 {result['clicks']}")
    expect(result["action_source"] == "declared", "来源应为 declared")
    expect(rec == [], "声明 grant 不该走拒绝")


@case("★多权限同框：拒绝一次后框内换下一个权限，继续按阶段推进")
def _():
    # 第一轮摄像头（拒）→ 第二轮换成照片（也是媒体类、但键不同 → 也拒）
    rec = []
    frames = [dialog_xml("日历正在尝试打开摄像头"),
              dialog_xml("日历正在尝试访问您设备上的照片"),
              None]
    result, _dev = run(frames, recorded=rec)
    texts = [c["text"] for c in result["clicks"]]
    expect(texts == ["拒绝", "拒绝"], f"两类权限各拒一次，实际 {texts}")
    expect(sorted(rec) == sorted(["日历正在尝试打开摄像头",
                                  "日历正在尝试访问您设备上的照片"]),
           f"两个键都要持久化，实际 {rec}")


@case("框只出现一次时不会误判（第二轮没有框就结束）")
def _():
    result, _dev = run([dialog_xml()])
    expect(result["dialog_count"] == 1, f"实际 {result['dialog_count']}")
    expect(len(result["clicks"]) == 1, f"实际 {result['clicks']}")


@case("phases 记录点下去的动作与键")
def _():
    result, _dev = run([dialog_xml(), None])
    expect(len(result["phases"]) == 1, f"实际 {result['phases']}")
    ph = result["phases"][0]
    expect(ph["action"] == "deny" and ph["clicked"] is True, f"实际 {ph}")
    expect(ph["key"] == "日历正在尝试打开摄像头", f"实际 {ph}")


@case("phases 在翻成允许时记 grant")
def _():
    result, _dev = run([dialog_xml(), None], denied=["日历正在尝试打开摄像头"])
    expect(len(result["phases"]) == 1, f"实际 {result['phases']}")
    expect(result["phases"][0]["action"] == "grant", f"实际 {result['phases']}")


@case("画面里没有权限框时 detected=False 且不点任何东西")
def _():
    result, dev = run([None])
    expect(result["detected"] is False, "不该检测到框")
    expect(dev.clicks == [], "不该点")


def main():
    failed = []
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as error:
            failed.append((name, str(error)))
            print(f"FAIL  {name}\n      {error}")
        except Exception as error:  # noqa: BLE001
            failed.append((name, f"{type(error).__name__}: {error}"))
            print(f"ERROR {name}\n      {type(error).__name__}: {error}")
        else:
            print(f"PASS  {name}")
    total = len(CASES)
    if failed:
        print(f"\n❌ {len(failed)}/{total} 条失败")
        return 1
    print(f"\n✅ 全部通过（{total} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
