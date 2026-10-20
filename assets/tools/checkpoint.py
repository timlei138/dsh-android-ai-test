"""检查点校验：动作执行后，判断画面是否落在**预期**的落点。

用途：`act.py --expect ...` 让工具代替 AI 做"这一步对不对"的判断 ——
AI 不必再花 10~15s 读 dump 自己认页面（那是会话时长的大头）。

期望的优先级（按"有 activity 变化就用 activity"的原则）：
    1. activity  —— 跨页跳转时最稳，也最能说明"走到哪了"
    2. rid       —— 同一 activity 的不同形态（如空态 / 周视图）用它区分
    3. text      —— 兜底

命中判定：全部给出的条件都满足才算命中（AND）。
"""

from __future__ import annotations


# 临时弹窗白名单：**每次不一定出现**的浮层（活动/引导/升级提示）。
# 判据是"是不是每次都在" —— 每次都在的属于**流程节点**（该进路径图），
# 不在这里；时有时无的才算临时弹窗，命中后自动关掉、继续走。
#
# ⚠️ 白名单只放"确定是临时的"。白名单外的意外画面**不猜**，交回给 AI 判断。
TRANSIENT_POPUP_RIDS = {
    # 系统弹框的通用键 —— 只在"该弹框不属于本用例主流程"时才该被关掉，
    # 所以调用方需显式允许（见 is_transient），不默认启用。
}

# 已知的 App 自有临时弹窗（版本性的引导/提示框）。
TRANSIENT_POPUP_SPECS = (
    # 课程表功能引导框（首次进入课程表时出现，清数据后才有）
    {"rid": "timetable_guide_button", "text": "我知道了"},
)


def parse_expect(spec: str | None) -> dict:
    """把 `--expect` 的字符串解析成条件字典。

    支持形如：
        activity=EditTimetableActivity
        rid=et_schedule_name
        text=完成
        activity=TimetableActivity,rid=btnCreateManually   （逗号分隔 = AND）
        activity=TimeSlotSettingsActivity|TimetableActivity （竖线 = 任一，OR）
    """
    if not spec or not spec.strip():
        return {}
    conds: dict[str, list[str]] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, val = part.partition("=")
        key = key.strip().lower()
        # 单字母简写 = 全名同义。YAML 里两种都有人写（`t=课程表` / `text=课程表`），
        # 而早先只认全名 -> 简写被**整条丢弃** -> expect 变空 -> 检查点
        # 永远"不校验"，等于判据静默失效（test_pace 就是这么挂的）。
        key = {"t": "text", "r": "rid", "d": "desc", "pkg": "package"}.get(key, key)
        # `package=` 是**唯一确定**的判据（前台包名相等即命中）：
        # 169 的 s2/s3 就写了 `expect.package: com.zui.camera`，
        # 早先它被当成"无法表达"直接丢掉，等于那条判据从来没生效过。
        if key not in ("activity", "rid", "text", "desc", "package"):
            continue
        vals = [v.strip() for v in val.split("|") if v.strip()]
        if vals:
            conds.setdefault(key, []).extend(vals)
    return conds


def evaluate(expect: dict, *, activity: str | None, sig: list[str] | None,
             package: str | None = None,
             ) -> dict:
    """按期望条件判定是否命中。

    返回 {"checked": bool, "ok": bool, "matched": [...], "missed": [...]}
    `checked=False` 表示没给期望（不校验）。
    """
    if not expect:
        return {"checked": False, "ok": True, "matched": [], "missed": []}

    act = (activity or "")
    sigset = set(sig or [])
    matched: list[str] = []
    missed: list[str] = []

    for key, vals in expect.items():
        key = {"t": "text", "r": "rid", "d": "desc", "pkg": "package"}.get(key, key)
        if key == "package":
            # 前台包名：**相等**比对，不是子串 —— `com.zui` 会误命中
            # `com.zui.calendar`，那等于没判。
            front = (package or "").strip()
            hit = bool(front) and any(front == v.strip() for v in vals)
            (matched if hit else missed).append(
                "package=" + "|".join(vals)
                + (f"（实际 {front or '未知'}）" if not hit else ""))
        elif key == "activity":
            # ⚠️ 匹配必须**按段落边界**，不能用 `endswith` 裸匹配：
            #   "...EditTimetableActivity".endswith("TimetableActivity") == True
            # 会让 EditTimetableActivity 误判成命中 TimetableActivity —— 检查点
            # 在**错误的页面**上静默通过，比不校验更危险（真实缺陷）。
            # 规则：全名相等，或待比项正好是完整类名段（前一个字符是 `.`）。
            def _act_hit(v: str) -> bool:
                if not act:
                    return False
                v = v.strip().lstrip(".")      # 允许写成 .a.B（路径图里就是这写法）
                if not v:
                    return False
                if act == v:
                    return True
                return act.endswith("." + v)
            hit = any(_act_hit(v) for v in vals)
            (matched if hit else missed).append(
                f"activity={'|'.join(vals)}" + (f"（实际 {act}）" if not hit else ""))
        else:
            hit = any(f"{key}={v}" in sigset for v in vals)
            (matched if hit else missed).append(
                f"{key}={'|'.join(vals)}")

    return {"checked": True, "ok": not missed, "matched": matched, "missed": missed}


def detect_transient(sig: list[str] | None) -> dict | None:
    """若当前画面是**已知的临时弹窗**，返回它的描述，供调用方决定是否关掉。

    只认白名单。**未知画面一律返回 None** —— 不猜，交回给 AI。
    """
    sigset = set(sig or [])
    for spec in TRANSIENT_POPUP_SPECS:
        rid = spec.get("rid")
        if rid and f"rid={rid}" not in sigset:
            continue
        return {"rid": rid, "text": spec.get("text"),
                "reason": "已知临时弹窗（非长期流程），可自动关闭后继续"}
    return None
