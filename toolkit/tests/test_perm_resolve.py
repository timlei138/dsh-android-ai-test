# -*- coding: utf-8 -*-
"""`tools/perm.py` 的权限名归一（`resolve_perm`）单测。

## 为什么必须测（N-32，真实缺陷）

原来这份短名表只在 `tools/state.py` 里，而 `tools/perm_tool.py` 自己写了个
`_full_name`：**只补前缀、不映射、不大写**。于是：

    perm_tool.py --grant media_images
      → pm grant <pkg> android.permission.media_images
      → 真名是 android.permission.READ_MEDIA_IMAGES，pm 静默失败
      → 但 apply() 只按输出里有没有 "Exception" 判成败，没输出就记 done
      → 调用方以为「已经授好了」，接着跑用例，实际还是没权限

这类错误最贵的地方是**不报错**：前面的授权动作"成功"，后面用例跑到空列表，
AI 只会以为产品坏了。所以这张表必须只有一份，且"猜不出来的一律大写"。

`resolve_perm` 是纯函数，不需要设备、不需要数据库。
"""
import io
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import perm as P  # noqa: E402

CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


@case("短名表：8 个短名全部映射到真实权限名")
def _():
    expected = {
        "camera": "android.permission.CAMERA",
        "location": "android.permission.ACCESS_FINE_LOCATION",
        "coarse_location": "android.permission.ACCESS_COARSE_LOCATION",
        "mic": "android.permission.RECORD_AUDIO",
        "contacts": "android.permission.READ_CONTACTS",
        "storage": "android.permission.READ_EXTERNAL_STORAGE",
        "media_images": "android.permission.READ_MEDIA_IMAGES",
        "notifications": "android.permission.POST_NOTIFICATIONS",
    }
    for short, full in expected.items():
        assert P.PERMS[short] == full, (short, P.PERMS[short], full)
        assert P.resolve_perm(short) == full, short


@case("★ media_images 不能退化成 android.permission.media_images（N-32 的原缺陷）")
def _():
    got = P.resolve_perm("media_images")
    assert got == "android.permission.READ_MEDIA_IMAGES", got
    # 反例必须长这样才证明修的是那个坑
    assert got != "android.permission.media_images"


@case("短名大小写不敏感（Camera / CAMERA / camera 同一结果）")
def _():
    for written in ("camera", "Camera", "CAMERA", "  camera  "):
        assert P.resolve_perm(written) == "android.permission.CAMERA", written


@case("★ 未入表的短名 → 补前缀并大写（read_calendar → READ_CALENDAR）")
def _():
    assert P.resolve_perm("read_calendar") == "android.permission.READ_CALENDAR"
    assert P.resolve_perm("READ_CALENDAR") == "android.permission.READ_CALENDAR"
    assert P.resolve_perm("write_calendar") == "android.permission.WRITE_CALENDAR"


@case("已带前缀的整名 → 原样返回，不改后半段大小写")
def _():
    assert P.resolve_perm("android.permission.CAMERA") == "android.permission.CAMERA"
    # 大小写写错的全名不该被"悄悄修正"：让 pm 报错比猜测更有用
    assert P.resolve_perm("android.permission.camera") == "android.permission.camera"


@case("空串 / 纯空白 / None → 空串（由调用方判，不在这里抛）")
def _():
    for empty in ("", "   ", "\t\n", None):
        assert P.resolve_perm(empty) == "", repr(empty)


@case("兜底不会出现「无前缀」或「小写后半段」")
def _():
    for written in ("read_sms", "READ_SMS", "access_network_state", "奇奇怪怪"):
        got = P.resolve_perm(written)
        assert got.startswith(P.PERMISSION_PREFIX), (written, got)
        tail = got[len(P.PERMISSION_PREFIX):]
        # 「奇奇怪怪」不是合法权限名，但也不能原样小写返回 —— 必须是全大写形态
        assert tail == tail.upper(), (written, got)


@case("幂等：已经归一过的值再归一不变")
def _():
    for written in ("media_images", "read_calendar", "android.permission.CAMERA"):
        once = P.resolve_perm(written)
        assert P.resolve_perm(once) == once, (written, once, P.resolve_perm(once))


@case("★ perm_tool._full_name 与 resolve_perm 同源（不能各写一套）")
def _():
    import perm_tool
    for written in ("media_images", "read_calendar", "camera",
                    "android.permission.CAMERA", "", "  mic  "):
        assert perm_tool._full_name(written) == P.resolve_perm(written), written


@case("perm_tool._split：逗号分隔 + 去空项 + 逐个归一")
def _():
    import perm_tool
    got = perm_tool._split("media_images, read_calendar, ,android.permission.CAMERA")
    assert got == [
        "android.permission.READ_MEDIA_IMAGES",
        "android.permission.READ_CALENDAR",
        "android.permission.CAMERA",
    ], got


@case("state.py 用的也是同一份表（不是自己再抄一遍）")
def _():
    import state
    assert state.PERMS is P.PERMS
    assert state.resolve_perm is P.resolve_perm


def main() -> int:
    print("=" * 70)
    print("tools/perm.py —— 权限名归一")
    print("=" * 70)
    failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as error:
            failed += 1
            detail = f"  {error}" if str(error) else ""
            print(f"  [FAIL] {name}{detail}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}  异常：{type(error).__name__}: {error}")
        else:
            print(f"  [PASS] {name}")
    print(f"\n  {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
