"""perm：系统权限弹窗的检测与响应（确定性，不依赖 AI 现场决策）。

设计参考 AiAgentTest 的 set_permission_intent 体系，解决一个硬约束：

    **系统权限框约 6 秒未点击会自动消失**（Android 通用机制），
    而"该同意还是该拒绝"需要读懂用例语义（慢）。
    决策与执行必须解耦——AI 事先声明意图（session.py perm-intent），
    工具在动作时按意图秒级响应。

匹配规则（rid 优先，与文案/语言无关）：
  grant：permission_allow_all_button(全量) > foreground_only > allow
  deny ：只认 permission_deny_button，**绝不点** deny_and_dont_ask_again
         （点了会设 don't-ask-again，导致后续"授予"分支弹窗不再出现）
不匹配的按钮（如"选择照片"部分媒体、"前往设置"）一律不点，交回 AI。
"""
from __future__ import annotations

import re
import time

# 系统权限弹窗的 activity 标记（大小写不敏感）
ACTIVITY_MARKERS = ("permissioncontroller", "grantpermissionsactivity")

# rid → 优先级（数字越小越优先）。grant 时优先"最小够用"的授权范围：
# 全量授权(1) 优先于 前台(2) 优先于 基本/单次(2 同级按出现顺序)
_GRANT_RID_SCORES = {
    "permission_allow_all_button": 1,            # 媒体「全部允许」
    "permission_allow_always_button": 1,         # 「始终允许」
    "permission_allow_foreground_only_button": 2,
    "permission_allow_button": 2,
    "permission_allow_one_time_button": 2,
}
_DENY_RID = "permission_deny_button"
_DONT_ASK_AGAIN_RID = "permission_deny_and_dont_ask_again_button"

# 文案兜底（非标准 OEM / 自定义 rid）
_GRANT_TEXTS = ("仅在使用中允许", "仅本次使用时允许", "始终允许", "全部允许",
                "允许访问所有照片", "允许", "同意", "Allow")
_DENY_TEXTS = ("拒绝", "不允许", "禁止", "Deny", "Don't allow")

# 明令排除：这些文案/rid 永不自动点
_EXCLUDE_TEXTS = ("选择照片", "仅选择照片", "允许访问所选照片", "前往设置",
                  "拒绝并不再询问", "拒绝且不再询问")
_EXCLUDE_RID_PARTS = (_DONT_ASK_AGAIN_RID, "dont_ask_again",
                      "permission_allow_selected_button")


def is_permission_activity(activity: str | None) -> bool:
    a = (activity or "").lower()
    return any(m in a for m in ACTIVITY_MARKERS)


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    m = re.search(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def list_buttons(xml_text: str) -> list[dict]:
    """从 UI 树里取出权限弹窗中"可见且有文字"的可点按钮。"""
    import xml.etree.ElementTree as ET
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for el in root.iter("node"):
        a = el.attrib
        if a.get("clickable") != "true":
            continue
        text = (a.get("text") or "").strip()
        if not text:
            continue
        c = _bounds_center(a.get("bounds", ""))
        if not c:
            continue
        out.append({"text": text, "rid": a.get("resource-id", ""),
                    "center": list(c)})
    return out


def _excluded(btn: dict) -> bool:
    if any(x in btn["text"] for x in _EXCLUDE_TEXTS):
        return True
    rid = btn["rid"]
    return any(p in rid for p in _EXCLUDE_RID_PARTS)


def match_button(buttons: list[dict], action: str) -> dict | None:
    """按 action 选目标按钮。返回按钮 dict 或 None（不匹配则交回 AI）。"""
    cands = [b for b in buttons if not _excluded(b)]
    if not cands:
        return None

    if action == "deny":
        # 只认精确的"拒绝"rid；文案兜底也只认 _DENY_TEXTS
        for b in cands:
            if b["rid"].endswith(_DENY_RID):
                return b
        for b in cands:
            if b["text"] in _DENY_TEXTS:
                return b
        return None

    # grant：rid 命中的按下标打分取最优；都没命中再文案兜底
    best, best_score = None, None
    for b in cands:
        for key, score in _GRANT_RID_SCORES.items():
            if b["rid"].endswith(key) and (best_score is None or score < best_score):
                best, best_score = b, score
                break
    if best:
        return best
    for t in _GRANT_TEXTS:
        for b in cands:
            if b["text"] == t:
                return b
    return None


# 系统权限弹窗的包名（固定，跨设备通用）。
#
# ★ 判断"这是谁的权限框"**只看包名**，不看文案 —— 用户明确说过：
#   「你要区分APP弹窗和系统权限弹窗。系统权限弹窗的包名是固定的。
#     APP弹窗的包名就是当前APP自己。」
#
# 曾经走错过：用"权限名 → 文案特征词"去猜归属。那是错的 ——
# 相机 App 弹的「相机正在尝试使用相机」里也有"相机"二字，
# 会被当成被测 App 的申请。包名唯一确定，文案不是。
SYSTEM_PERM_PACKAGES = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.packageinstaller",
)


def is_system_perm_package(pkg: str | None) -> bool:
    """这个包名是不是系统权限弹窗？"""
    p = (pkg or "").strip()
    return bool(p) and any(p == m or p.startswith(m + ".")
                           for m in SYSTEM_PERM_PACKAGES)


def granted_permissions(d, package: str,
                        timeout_s: float = 6.0) -> dict[str, bool]:
    """查某个 App **当前被授予了哪些运行时权限**。返回 {权限名: bool}。

    为什么需要它 —— 用户的原则：
        「我们测试是测试APP。同意了才能打开相机。只要打开就行了。
          哪怕打开后相机APP权限。那不是我们这个case关心的。」

    也就是说，用例要验的是"**被测 App** 的权限有没有被授予"，
    而不是"屏幕上有没有出现过某个弹窗"。弹窗只是过程，授权才是结果。

    实现：`dumpsys package <pkg>` 里有两段
        requested permissions:      ← 声明了哪些（granted 与否无关）
        runtime permissions: ... granted=true/false
    只取被授予的那些。

    ⚠️ 用 subprocess 直接调 adb，**不新增设备依赖** ——
       这条路径要能离线单测（传一个假的 dumpsys 输出即可验证解析）。
    取不到时返回空 dict（调用方据此判"判不了"，而不是猜）。
    """
    import subprocess

    serial = getattr(d, "serial", None) or getattr(d, "_serial", None) or ""
    cmd = ["adb"]
    if serial:
        cmd += ["-s", str(serial)]
    cmd += ["shell", "dumpsys", "package", package]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        text = (r.stdout or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return {}
    return parse_granted_permissions(text)


def parse_granted_permissions(dumpsys_text: str) -> dict[str, bool]:
    """从 `dumpsys package <pkg>` 的文本里解析出已授予的运行时权限。

    只看**运行时权限**段（段名 `runtime permissions:` 或 `install permissions:`），
    形如：
        android.permission.CAMERA: granted=true, flags=[USER_SET|...]

    ⚠️ 不要扫全文：`requested permissions:` 段里也会出现权限名，
       那里没有 granted 标记，混进来会把"声明了"当成"已授予"。
    """
    out: dict[str, bool] = {}
    in_runtime = False
    for raw in (dumpsys_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        # 段头
        if low.startswith(("runtime permissions:", "install permissions:",
                           "requested permissions:")):
            in_runtime = low.startswith(("runtime permissions:",
                                         "install permissions:"))
            continue
        if not in_runtime:
            continue
        # 段内：权限名: granted=true, flags=[...]
        m = re.match(r"^([\w.]+):\s*granted=(true|false)", line)
        if m:
            out[m.group(1)] = (m.group(2) == "true")
    return out


def permission_prompt(xml_text: str) -> str:
    """取权限框里"正在申请哪个权限"的文案，**仅用于留痕**。

    形如「日历正在尝试读取联系人」。取不到返回空串。
    不做任何判断依据 —— 点击逻辑只看"框还在不在"。
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except Exception:  # noqa: BLE001
        return ""
    for el in root.iter("node"):
        t = (el.attrib.get("text") or "").strip()
        # 申请语句特征；排除按钮自身文案
        if t and ("正在尝试" in t or "想要" in t) and t not in _GRANT_TEXTS + _DENY_TEXTS:
            return t
    return ""


def detect(d, timeout_s: float = 0.8, interval_s: float = 0.2) -> dict | None:
    """有界轮询检测权限弹窗。

    返回 {"activity","buttons","package","prompt"} 或 None；只读不点。
    `prompt` 仅作留痕（本次申请的是哪个权限），**不参与任何判断**。

    ⚠️ 默认窗口刻意取小（0.8s）：**屏幕上没有权限弹窗时也要空等到 deadline**，
    而每步 act/observe 都会调用一次 —— 3s 的默认值等于每步白等 3 秒。
    权限弹窗是**动作的直接结果**，渲染延迟 100~300ms，0.8s 已经足够；
    确实慢的设备用 timeout_s 显式加大（handle 的第二轮用 2s 兜底）。
    """
    deadline = time.time() + max(0.0, min(timeout_s, 8.0))
    while True:
        try:
            cur = d.app_current() or {}
            act = cur.get("activity") or ""
            if is_permission_activity(act):
                xml = d.dump_hierarchy()
                btns = list_buttons(xml)
                # activity 对了但还没渲染出按钮时继续等
                if btns:
                    return {"activity": act, "buttons": btns,
                            "package": cur.get("package"),
                            "prompt": permission_prompt(xml)}
        except Exception:  # noqa: BLE001
            pass
        if time.time() >= deadline:
            return None
        time.sleep(interval_s)


# 未声明意图时的默认动作。
# 规则（人明确要求）：**默认同意**——大多数 GUI 用例测的是"功能能不能用"，
# 授权只是通往功能的前置，不是被测对象；默认不点会让权限框 6 秒后自行消失，
# 后续步骤全部走不下去。
# 例外：用例明确要测"拒绝路径"时，用 session.py perm-intent --action deny 覆盖。
DEFAULT_ACTION = "grant"


def _frame_signature(info: dict | None) -> str:
    """给"这一屏"算个简单指纹：申请文案 + 按钮文案。

    只用来判断**点完之后屏幕变了没有**（和人的做法一样：点一下，看有没有反应）。
    不做"这是不是同一个框"的判断 —— 多权限时按钮全叫「允许」，判不出来。
    """
    if not info:
        return ""
    parts = [str(info.get("prompt") or "")]
    for b in info.get("buttons") or []:
        parts.append(str(b.get("text") if isinstance(b, dict) else b))
    return "|".join(parts)


def handle(d, action: str | None = None, timeout_s: float = 0.8,
           chain_max: int = 8, observe_only: bool = False,
           owner_package: str = "") -> dict:
    """看见权限框就点，点到框没了为止 —— 和测试员的做法完全一致。

    多权限申请时 Android **不关框**，就在同一个框里点一次换下一个权限，
    所以循环判据只有一条：**框还在就继续点**。`chain_max` 只是防死循环的兜底。

    action=None → 用 DEFAULT_ACTION（grant，默认同意）。
    observe_only=True → 只检测不点击（调试/仅取证时用）。

    owner_package: 动作执行前的前台包 = **这个权限框是谁申请的**。
        为什么需要它（真机踩过）：用例预期是"点拍照导入应弹相机权限弹窗"，
        而实际弹的是**日历自己的**「日历正在尝试打开摄像头」。两者都叫
        "相机权限"，但归属完全不同 —— 前者是被测 App 的（要验），
        后者是相机 App 的（不关本用例的事）。不记归属，事后无从分辨，
        就会出现"权限明明弹了、也允许了，却判 FAIL"。
        取不到时为空串（不影响点击，只是少了归属信息）。
    返回 {"detected","handled","clicks","dialog_count",...}
    """
    result = {"detected": False, "handled": False, "clicks": [],
              "dialog_count": 0, "activity": None, "buttons": [], "prompts": []}
    if owner_package:
        result["owner_package"] = owner_package
    if not observe_only:
        result["action"] = action or DEFAULT_ACTION
        result["action_source"] = "declared" if action else "default"
    for i in range(max(1, chain_max)):
        info = detect(d, timeout_s=timeout_s if i == 0 else 2.0)
        if not info:
            break                       # 框没了 = 点完了（或本来就没有）
        result["detected"] = True
        result["dialog_count"] = i + 1
        result["activity"] = info["activity"]
        result["buttons"] = [b["text"] for b in info["buttons"]]
        if info.get("prompt"):
            result["prompts"].append(info["prompt"])   # 留痕：本次授予了哪些权限
        if observe_only:
            break                       # 只看不点
        btn = match_button(info["buttons"], action or DEFAULT_ACTION)
        if not btn:
            result["unmatched"] = True  # 框在，但没有可点的匹配项 → 交回 AI
            break
        # ⚠️ 点完看**框变了没有** —— 和人的做法一样：点一下，看有没有反应。
        # 变了 = 换成下一个权限/关了；没变 = 点不动（disabled/遮挡），立刻停。
        # 不做"这是不是同一个框"的判断（多权限时按钮全叫「允许」，判不出来）。
        before = _frame_signature(info)
        try:
            d.click(*btn["center"])
            result["clicks"].append({"text": btn["text"], "rid": btn["rid"],
                                     "center": btn["center"]})
            result["handled"] = True
        except Exception as e:  # noqa: BLE001
            result["click_error"] = str(e)
            break
        time.sleep(0.5)                 # 给框内换下一个权限留渲染时间
        after = detect(d, timeout_s=1.0)
        if after is None:
            break                       # 框关了 → 正常结束
        if _frame_signature(after) == before:
            # 屏幕内容和点之前一模一样 → 这一下没生效，别再连点（最坏会白等 18s）
            result["stuck"] = True
            break
    return result
