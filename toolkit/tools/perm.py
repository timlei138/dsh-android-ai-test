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


# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ⚠️/✅ 就崩。
#   必须在任何输出之前调用（含 argparse 的报错输出）。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass

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


def detect_from_xml(xml_text: str) -> dict | None:
    """从**已经 dump 下来的 xml** 判断这是不是一个权限弹窗。

    返回 {"activity": None, "buttons": [...], "package": None, "prompt": "..."}
    或 None（不是权限框）。

    ## 为什么要有这条路径

    原来点完权限框后要再跑一次 `detect()` 去 `dumpsys` 查前台 activity，
    最多白等 1 秒。而调用方（act）点完之后**本来就要 dump + 截图**存档 ——
    那份 xml 里有没有权限按钮，就是「框还在不在」的事实本身。
    用现成的 xml 判断：0 额外开销，而且不受页面过渡期 `dumpsys` 返回旧值的
    干扰（`common.foreground_of` 的坑，见那里的注释）。

    ⚠️ 拿不到 activity，所以只按**按钮形态**判断。这是有意的：判断依据是
       「屏幕上有没有可点的权限按钮」，而不是「前台是不是权限 Activity」——
       前者才是用户能看见的事实。

    ⚠️ 但普通页面也有按钮（「新建」「设置」），光看「有按钮」不算数。
       所以额外要求：xml 里出现 `permissioncontroller` 这个包名 —— 权限控制器
       渲染的所有控件 rid 都带它，普通 App 的页面不可能有。这是**权限框的
       物理标记**，不是靠文案猜的（换语言/换 OEM 也不受影响）。
    """
    if "permissioncontroller" not in (xml_text or ""):
        return None
    btns = list_buttons(xml_text)
    if not btns:
        return None
    return {"activity": None, "buttons": btns, "package": None,
            "prompt": permission_prompt(xml_text)}


def detect(d, timeout_s: float = 0.8, interval_s: float = 0.2) -> dict | None:
    """有界轮询检测权限弹窗。

    返回 {"activity","buttons","package","prompt"} 或 None；只读不点。
    `prompt` 仅作留痕（本次申请的是哪个权限），**不参与任何判断**。

    ⚠️ 默认窗口刻意取小（0.8s）：**屏幕上没有权限弹窗时也要空等到 deadline**，
    而每步 act/observe 都会调用一次 —— 3s 的默认值等于每步白等 3 秒。
    权限弹窗是**动作的直接结果**，渲染延迟 100~300ms，0.8s 已经足够；
    确实慢的设备用 timeout_s 显式加大（handle 的第二轮用 2s 兜底）。

    ⚠️ 取 activity 用 `foreground_of()`（走 dumpsys），**不是 u2 的 app_current**：
      后者在本 ROM 上要好几秒（`dumpsys window windows` 没有 mCurrentFocus，
      退回 `dumpsys activity top`），而每步都调一次等于白等。
      判断逻辑一个字没改 —— 仍然是 `is_permission_activity(activity)`。
    """
    from common import foreground_of
    deadline = time.time() + max(0.0, min(timeout_s, 8.0))
    while True:
        try:
            cur = foreground_of(d) or {}
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


def handle(d, action: str | None = None, shot=None, on_dialog=None,
           settle_s: float = 0.3, chain_max: int = 8) -> dict:
    """看见权限框就点，点到框没了为止 —— 和测试员的做法完全一致。

    多权限申请时 Android **不关框**，就在同一个框里点一次换下一个权限，
    所以循环判据只有一条：**框还在就继续点**。`chain_max` 只是防死循环的兜底。

    action=None → 用 DEFAULT_ACTION（grant，默认同意）。

    ## 每一轮只拍一次：截图 + dump 都在调用方

        调用方点完按钮、等够了结算时间 → 调 shot() 拍一份
          → 这份 dump 里有没有权限按钮？没有 → 结束
          → 有 → 点掉 → 等 settle_s → 下一轮再拍一份

    `shot()` 返回 `(xml_text, payload_dict)`，payload 是给 AI 的证据描述。
    **第一份也是 shot() 拍的**，不是 detect() —— 调用方已经等过结算时间，
    那时权限框早就渲染完了，direct dump 就是权限框原样；再去 dumpsys 轮询
    一遍纯属白花时间。

    `on_dialog(info, btn)`：每**点掉**一个弹窗回调一次（2026-11-06 裁决：
    「点过的就是当做一个步骤」）。此刻调用方 step 目录里那份 png/xml/meta
    正是这个弹窗的画面 —— 调用方把它复制去独立目录记一步即可，**不要再拍**，
    下一轮 shot() 会自然覆盖 act 目录。回调异常不中断点框流程。

    ⚠️ 为什么不在这里自己截图/dump：那张图要成为本次 act 的证据，必须落在
      调用方的 step 目录、走 capture() 的落盘链路。在这里拍等于同一时刻拍
      两份（一份白丢，还要多花一次截图的钱）。

    ⚠️ 判断框在不在，不看 activity 名，只看 **xml 里有没有权限控制器渲染的
      按钮**（`detect_from_xml`）：0 额外开销，且不受页面过渡期 dumpsys
      返回旧值的影响。

    返回 {"detected","handled","clicks","dialog_count",...,"prompts"}
      `detected=False` 表示拍的第一份画面里就没有权限框 —— 本次动作与权限无关
      `clicks` 是「点了哪几个权限」：每项 {text, rid, center}
    """
    result = {"detected": False, "handled": False, "clicks": [],
              "dialog_count": 0, "buttons": [], "prompts": []}
    result["action"] = action or DEFAULT_ACTION
    result["action_source"] = "declared" if action else "default"
    if shot is None:
        result["error"] = "handle() 需要 shot 回调"
        return result

    before = None                       # 上一轮点之前的画面指纹
    for i in range(max(1, chain_max)):
        try:
            xml_text, _payload = shot()
        except Exception as e:  # noqa: BLE001
            result["shot_error"] = str(e)
            break
        info = detect_from_xml(xml_text)
        if info is None:
            break                       # 画面里没有权限框 → 结束
        result["detected"] = True
        result["dialog_count"] = i + 1
        result["buttons"] = [b["text"] for b in info["buttons"]]
        if info.get("prompt"):
            result["prompts"].append(info["prompt"])   # 留痕：本次授予了哪些权限

        # 点了没反应：和上一轮点之前的画面一模一样 → 这一下没生效（disabled/遮挡）。
        # 用户 2026-11-06 的裁决：「这种情况不会发生。即使发生了报 BLOCK 就行了。」
        sig = _frame_signature(info)
        if before is not None and sig == before:
            result["stuck"] = True
            break

        btn = match_button(info["buttons"], result["action"])
        if not btn:
            result["unmatched"] = True  # 框在，但没有可点的匹配项 → 交回 AI
            break
        before = sig
        try:
            d.click(*btn["center"])
            result["clicks"].append({"text": btn["text"], "rid": btn["rid"],
                                     "center": btn["center"]})
            result["handled"] = True
        except Exception as e:  # noqa: BLE001
            result["click_error"] = str(e)
            break
        # 点掉一个 = 一个步骤。此刻调用方目录里那份画面就是这个弹窗，
        # 复制去独立目录留证（不重拍）；下一轮 shot() 会覆盖调用方目录。
        if on_dialog:
            try:
                on_dialog(info, btn)
            except Exception:  # noqa: BLE001  留证失败不该中断点框
                pass
        time.sleep(settle_s)            # 给框内换下一个权限留渲染时间
    return result
