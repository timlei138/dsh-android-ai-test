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



# ★ UTF-8 兜底：Windows 控制台默认 GBK，print 一个 ⚠️/✅ 就崩。
#   必须在任何输出之前调用（含 argparse 的报错输出）。
try:
    from common import ensure_utf8_stdio
    ensure_utf8_stdio()
except Exception:  # noqa: BLE001  common 不可用时不要因此崩掉
    pass

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
        if key not in ("activity", "rid", "text", "desc"):
            continue
        vals = [v.strip() for v in val.split("|") if v.strip()]
        if vals:
            conds.setdefault(key, []).extend(vals)
    return conds


def evaluate(expect: dict, *, activity: str | None, sig: list[str] | None,
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
        if key == "activity":
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
