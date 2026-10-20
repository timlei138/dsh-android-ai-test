"""首次跑完 -> 生成用例 YAML。

★ 设计依据（docs/执行机制.md 3.5）：

    「如果这个Case 第一次跑完了会生成YAML。」

    「YAML 也只是作为一个第一级别的参考文件。不同设备或者分辨率的回放有问题。
      YAML 里严谨存储坐标信息 —— 这个非常不可靠的东西。」

所以这里**只写结构、不写坐标**：
    desc      这步干什么（由执行过程拼出来，不是模型事后回忆）
    target    这步点的什么控件（rid / xpath，与分辨率无关）
    landing   落点判据（代码能唯一确定的那些）
    pitfalls  踩到的坑

为什么不写坐标：换一台设备、换一个分辨率、换一次布局，坐标全废，
而 `[121,1086]` 在另一台机器上可能落在完全不相干的控件上。
写了坐标的 YAML 看起来更"完整"，实际是给后来人挖坑。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

# YAML 里允许出现的动作形态 —— 坐标不在其中
_ACTION_KEYS = ("rid", "text", "desc", "xpath", "key")


def _spec_to_actions(item: dict[str, Any]) -> str | None:
    """把一次实际动作转成 YAML 的 target 表达式。

    只认能**跨设备**表达的形态。全是盲点坐标的那次点击返回 None ——
    宁可这一步不写 target（下次靠模型判断），也不要写一个错坐标进去。

    ★ 系统权限框上的点击同样返回 None：那是 Android 标准流程，各 App 一样。
      权限点过什么记在 report.runtime_permissions，不进用例 YAML。
    """
    from .writeback import _is_perm_button
    if _is_perm_button(item):
        return None

    key = str(item.get("key") or "").strip()
    if key:
        return f"key={key}"

    xp = str(item.get("xpath") or "").strip()
    if xp:
        return f"xpath={xp}"

    rid = str(item.get("rid") or item.get("r") or "").strip()
    if rid:
        # 只留末段：com.xxx:id/iv_more -> iv_more
        return f"rid={rid.split('/')[-1]}"

    text = str(item.get("text") or item.get("t") or "").strip()
    if text:
        return f"text={text}"

    # ★ 走到这里说明这次点击只有坐标 —— 刻意返回 None，不写 x=/y=
    return None


def _compose_desc(step_actions: list[dict[str, Any]],
                  landing: str = "", noise: list[str] | None = None) -> str:
    """用**实际发生了什么**拼出 desc。

    为什么不事后让模型总结：模型只能凭记忆写"打开课程表"这种话，
    下次跑遇到问题，凭它什么都判断不了。
    而轨迹里有现成的事实：点了哪个控件、中途冒出过什么、最后落到哪。

    例：
        点 iv_more，中途出现权限框，落到 TimetableActivity
    """
    parts: list[str] = []
    acts = [a for a in (_spec_to_actions(x) for x in step_actions) if a]
    if acts:
        seen: list[str] = []
        for a in acts:
            if a not in seen:
                seen.append(a)
        parts.append("点 " + " → ".join(seen))
    if noise:
        parts.append("中途" + "、".join(noise[:2]))
    if landing:
        parts.append(f"落到 {landing}")
    return "，".join(parts) or "（本次未记录到明确动作）"


def _landing_of(step: Any) -> str:
    """取这一步的落点判据。

    只保留**代码能唯一判定**的形态（activity / rid / xpath）——
    其余判据代码判不了，写进 YAML 也只是摆设。
    """
    land = str(getattr(step, "landing", None) or "").strip()
    if not land:
        return ""
    from ..agent.nodes import is_deterministic_landing
    ok, _ = is_deterministic_landing(land)
    return land if ok else ""


def render_case_yaml(spec: Any, *, trace: list[dict[str, Any]] | None = None,
                     pitfalls: dict[str, list[str]] | None = None,
                     status: str = "") -> str:
    """把一次执行的结果渲染成用例 YAML 文本。

    trace 每条形如 {"step": "s1", "actions": [{rid|text|key|...}]}
    pitfalls 每步踩到的坑 {step_id: [字符串]}
    """
    by_step: dict[str, list[dict[str, Any]]] = {}
    for t in (trace or []):
        sid = str(t.get("step") or "")
        if sid:
            by_step.setdefault(sid, []).extend(t.get("actions") or [])

    lines: list[str] = []
    add = lines.append

    add("# 由执行过程自动生成 —— 只记结构与判据，**不记坐标**。")
    add("# 坐标换设备/分辨率即失效，所以本文件里刻意不出现任何 x=/y=。")
    add(f"# 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    if status:
        add(f"# 本次执行结论：{status}")
    add("")

    add("meta:")
    add(f"  case_id: {_q(getattr(spec, 'case_id', ''))}")
    add(f"  title: {_q(getattr(spec, 'title', ''))}")
    add(f"  package: {_q(getattr(spec, 'package', ''))}")
    kfs = list(getattr(spec, "knowledge_files", None) or [])
    if kfs:
        add("  knowledge_files:")
        for k in kfs:
            add(f"    - {_q(k)}")
    add("")

    # ── 前提 ──
    preconds = list(getattr(spec, "preconditions", None) or [])
    if preconds:
        add("preconditions:")
        for pc in preconds:
            kind = getattr(pc, "kind", "") or "data"
            add(f"  - kind: {kind}")
            desc = str(getattr(pc, "desc", "") or "").strip()
            if desc:
                add(f"    desc: {_q(desc)}")
            cmd = str(getattr(pc, "cmd", "") or "").strip()
            if cmd:
                add(f"    do: {_q(cmd)}")
            undo = str(getattr(pc, "undo", "") or "").strip()
            if undo:
                add(f"    undo: {_q(undo)}")
        add("")

    # ── 步骤 ──
    steps = list(getattr(spec, "steps", None) or [])
    if steps:
        add("steps:")
        for st in steps:
            sid = str(getattr(st, "id", "") or "")
            add(f"  - id: {sid}")

            acts = by_step.get(sid) or []
            landing = _landing_of(st)
            pits = (pitfalls or {}).get(sid) or []

            desc = _compose_desc(acts, landing)
            # 用例原本写的 desc 更贴近"意图"，自动生成的更贴近"事实"。
            # 两个都有就合并：意图在前，事实补充在后。
            orig = str(getattr(st, "desc", "") or "").strip()
            if orig and orig not in desc:
                desc = f"{orig}｜{desc}"
            add(f"    desc: {_q(desc)}")

            # target：这次实际点的东西（表达不出来就不写）
            targets: list[str] = []
            for a in acts:
                t = _spec_to_actions(a)
                if t and t not in targets:
                    targets.append(t)
            if len(targets) == 1:
                add(f"    target: {_q(targets[0])}")
            elif len(targets) > 1:
                # 多种走法 → 都记下来，但标明这只是"上次走过的一种"
                add("    targets_tried:")
                for t in targets:
                    add(f"      - {_q(t)}")

            if landing:
                add(f"    landing: {_q(landing)}")

            if pits:
                add("    pitfalls:")
                for p in pits:
                    add(f"      - {_q(p)}")
            add("")

    # ── 断言 ──
    asserts = list(getattr(spec, "asserts", None) or [])
    if asserts:
        add("assertions:")
        for a in asserts:
            d = a if isinstance(a, dict) else {"id": getattr(a, "id", ""),
                                               "desc": getattr(a, "desc", "")}
            add(f"  - id: {d.get('id', '')}")
            add(f"    desc: {_q(str(d.get('desc', '')))}")
            check = d.get("check")
            if check is not None:
                if isinstance(check, str) and check:
                    add(f"    check: {_q(check)}")
                elif isinstance(check, dict) and check:
                    add("    check:")
                    for k, v in check.items():
                        add(f"      {k}: {_q(str(v))}")
            add("")

    return "\n".join(lines).rstrip() + "\n"


def _q(v: str) -> str:
    """把一个字符串写成 YAML 标量。含特殊字符就加引号。"""
    s = str(v)
    if not s:
        return '""'
    need = any(c in s for c in ':#{}[],&*?|-<>=!%@`\'"') or s != s.strip()
    if need:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def write_case_yaml(spec: Any, path: str | Path, *,
                    trace: list[dict[str, Any]] | None = None,
                    pitfalls: dict[str, list[str]] | None = None,
                    status: str = "") -> Path:
    """首次跑完 -> 落盘生成 YAML。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_case_yaml(spec, trace=trace, pitfalls=pitfalls,
                                  status=status), encoding="utf-8")
    return p
