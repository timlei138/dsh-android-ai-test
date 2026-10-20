# -*- coding: utf-8 -*-
"""把一次真实执行的路径回写进用例 YAML。

用户要求（原话）：
  「用我们的工具完成后要生成或者把旧的YAML不合理的修改。」

## 设计原则

1. **只在有差异时改**。实际路径和 YAML 声明一致就一个字都不动 ——
   否则每次跑完都改一遍文件，diff 全是噪音，没人敢看。

2. **改的是"建议"，不是"断言"**。YAML 在新方案里只是参考（用户第 1 条），
   所以这里只更新 `do:`/`locate_seq` 这类"上次怎么走的"，
   **绝不碰** `expect:`/`assertions:` —— 那是用例的判定标准，属于人写的需求。
   工具的职责是记录路径，不是替用户改验收标准。

3. **保留注释和原文结构**。YAML 里那些坑（pitfalls）和注解是真实经验，
   不能因为重写文件就丢掉。所以**不整文件重新 dump**，
   只在原文基础上做**逐行替换**。

4. **改动必须可审计**。报告里列清楚"哪一步、原来写什么、实际是什么"，
   主 Agent 能直接转述给用户。

## 为什么单独一个模块

case_yaml.py 的职责是"读 YAML → 用例对象"，只读不写。
回写是相反的流向（执行结果 → 文件），失败也不该影响用例结论，
放一起会让"读失败"和"写失败"互相污染。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 只回写这些"路径类"字段 —— 判定标准绝不动（见模块注释第 2 条）
_PATH_KEYS = ("do", "locate")

# ⚠️ 曾经想在这里过滤"引导框动作"（button1 / timetable_guide_button），
#    理由是它们污染了 s1 的定位。**那是错的，已撤销**：
#    · 实测 169 的 s1 轨迹就是 button1 → timetable_guide_button → iv_more → 课程表，
#      前两个是**冷启动必经**（权限说明框 + 课程表引导页），不是噪音
#    · 本项目**每次都是冷启动**（pm clear），所以引导框每次都会出现，
#      过滤掉等于把必经步骤从用例里抹掉，下次还得模型现场摸索
#    · 当时以为"回写把引导混进了 s1"，其实 report 里 yaml_writeback 是
#      `changed: []` —— 文件压根没被改过，是我误判
#    ⇒ 正确做法：引导框属于**独立步骤**（s0 的 do），不该被过滤，
#      也不该混进 s1。要区分的是"哪一步"，不是"删掉它"。


def _is_perm_button(item: dict[str, Any]) -> bool:
    """这个动作是不是"点系统权限框上的按钮"？

    ## 为什么权限动作**一律不写进 YAML**（用户明确要求）

    用户原话：
        「这种权限的我们只点击记录点击了哪些个权限。
          操作步骤不记录到 yaml 里面。因为都是按照标准走。」

    道理：系统权限框是 **Android 平台的标准流程**，每个 App 都一样 ——
    「拒绝 / 允许」两个按钮、rid 固定、位置由系统决定。
    把它写进用例 YAML 没有任何信息量，只会：
      · 让用例文件变脏，人看不出哪几步才是这个 App 的业务链路
      · 把"平台的通用行为"冒充成"这个 App 的经验"，误导后来人
      · 跨 Android 版本时 rid 变了，写死的判据反而失效

    而权限**点过什么**必须留下来 —— 那是不可逆的现场变更，审计要查。
    所以：留在 `report.runtime_permissions` / `runtime_permission_prompts`，
    **不进 YAML**。记录与写入是两个目的，不该混在一起。
    """
    rid = str(item.get("rid") or "")
    if rid:
        low = rid.lower()
        if any(k in low for k in ("permission_allow", "permission_deny",
                                  "permissioncontroller",
                                  "permission_grant", "permission_dont_ask")):
            return True
    # 文案兜底：系统权限框的按钮文案是固定的那几个
    text = str(item.get("text") or "").strip()
    if text in ("允许", "拒绝", "全部允许", "仅在使用中允许", "仅本次允许",
                "始终允许", "不允许", "拒绝并不再询问", "Allow", "Deny"):
        return True
    return False


def _actual_locate(item: dict[str, Any]) -> str | None:
    """把一次实际动作转成 locate 表达式（和 YAML 的写法对齐）。

    只认能可靠表达的形态；表达不了的（纯坐标点空白处）返回 None，
    **不要**把 `x=123,y=456` 写进 YAML —— 坐标换设备必失效，
    写进去反而是给后来人挖坑。

    ★ 系统权限框上的点击**一律返回 None**（见 `_is_perm_button`）：
      那是 Android 标准流程，各 App 一样，写进 YAML 没有信息量。
      权限记录走 report.runtime_permissions，不进用例文件。
    """
    if _is_perm_button(item):
        return None
    if item.get("key"):
        return f"key={item['key']}"
    rid = (item.get("rid") or "").strip()
    if rid:
        return f"rid={rid}"
    text = (item.get("text") or "").strip()
    if text:
        return f"text={text}"
    return None


def diff_steps(spec: Any, trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对比 YAML 声明的每步目标 vs 实际走过的，只返回**有差异**的步。

    trace 每条形如：
        {"step": "s3", "actions": [{"rid": "..."} | {"key": "back"} ...]}

    ★★ 只拿**代码强证据直点**的动作回写，模型的点击一概不采信。
    ─────────────────────────────────────────────────────────────
    这是真机踩出来的（168 跑完 PASS，写回却把 s1 的 iv_more 改成了
    rid=button1 —— 那是 s0 点「同意」的按钮，被模型在 s1 里又点了一次）。

    为什么必须这么严：
      回写的目的，是把「代码能唯一确定的目标」沉淀下来，下次少问模型。
      模型摸索出来的点击恰恰是**最不可靠**的那部分 —— 它可能点的是
      上一屏遗留的按钮、可能凑巧点中了、可能只是当下这一屏恰好如此。
      把这种动作写进 YAML，等于把偶然固化成"经验"，下次照着它跑更错。

    所以：trace 里的动作只有标了 by="code" 的才参与比对。
    代码没直点过的步 -> 不比；模型改走过的路径 -> 不回写。

    「差异」的判定（保守）：
      · 实际轨迹里没有这一步            -> 不比（没跑过就谈不上对错）
      · 实际动作不是代码直点的          -> 不比（模型的是猜测，不是经验）
      · 实际动作一个都表达不出来        -> 不比（全是盲点坐标，无从回写）
      · 实际序列 == YAML 序列           -> 不比（一字不改）
      · 其余                            -> 记为差异，给出建议新值
    """
    by_step: dict[str, list[str]] = {}
    for t in trace or []:
        sid = str(t.get("step") or "")
        # ★ 只看代码直点的动作。缺 by 字段时**按模型处理**（不采信）——
        #   宁可少回写，也不要把猜测写进用例资产。
        if str(t.get("by") or "") != "code":
            continue
        acts = [a for a in (_actual_locate(x) for x in (t.get("actions") or []))
                if a]
        if sid and acts:
            by_step[sid] = acts

    changes: list[dict[str, Any]] = []
    for st in (getattr(spec, "steps", None) or []):
        sid = getattr(st, "id", "")
        actual = by_step.get(sid)
        if not actual:
            continue
        declared = list(getattr(st, "locate_seq", None) or [])
        if not declared:
            one = (getattr(st, "target", None) or getattr(st, "locate", None) or "").strip()
            declared = [one] if one else []
        if declared == actual:
            continue
        changes.append({
            "step": sid,
            "declared": declared,
            "actual": actual,
            "reason": ("YAML 写的目标这次没找到，实际是这么走过去的"
                       if not set(actual) & set(declared)
                       else "实际路径与 YAML 声明不同"),
        })
    return changes


def _yaml_target_line(loc: str) -> str | None:
    """把 locate 表达式渲染成 `target:` 的值（新规范，单值）。

    ★ 新写法只写一个 target —— 代码拿它去当前屏找，
      命中恰好 1 个才直点，0 个或 >1 个交模型。
      所以这里挑**最可靠的那一个**：rid > xpath > text > key。

    返回 None = 这次动作表达不出来（纯坐标点击），不要写进 YAML。
    """
    if loc.startswith("xpath="):
        return loc
    if loc.startswith("rid="):
        return loc
    if loc.startswith("text="):
        text = loc[5:].strip()
        return f"text={text}"
    if loc.startswith("key="):
        return loc
    return None


def _yaml_tap_line(loc: str) -> str | None:
    """把 locate 表达式渲染成 YAML 的 `do:` 条目行（不带缩进）。

    ⚠️ 这是**旧格式**的渲染，保留是为了还能修补老 YAML。
       新生成的用例用 `target:` 单值，见 `_yaml_target_line`。
    """
    if loc.startswith("key="):
        k = loc[4:].strip()
        return f"- {k}: true"
    if loc.startswith("rid="):
        return f"- tap: {{rid: {loc[4:].strip()}}}"
    if loc.startswith("text="):
        v = loc[5:].strip()
        return f'- tap: {{text: "{v}"}}'
    return None


def writeback(path: str | Path, changes: list[dict[str, Any]],
              dry_run: bool = False) -> dict[str, Any]:
    """把差异写回 YAML 文件。**逐行替换，保留所有注释。**

    返回 {"changed": [...], "skipped": [...], "path": str}

    实现说明：为什么是逐行替换而不是 yaml.safe_load + dump ——
    用例 YAML 里大段注释（尤其 pitfalls 里的真实经验）在 dump 后会全丢，
    那比不改更糟。
    """
    p = Path(path)
    if not changes:
        return {"changed": [], "skipped": [], "path": str(p)}
    if not p.exists():
        return {"changed": [], "skipped": [f"{p} 不存在"], "path": str(p)}

    text = p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    changed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for ch in changes:
        sid = ch["step"]

        # 找到 `- id: sX` 这一行
        start = None
        for i, ln in enumerate(lines):
            if re.match(rf"^\s*-\s*id:\s*{re.escape(sid)}\s*$", ln.rstrip("\n")):
                start = i
                break
        if start is None:
            skipped.append({"step": sid, "why": "YAML 里没找到该步的 `- id:` 行"})
            continue

        # ── 范围：本步的 id 行到下一步的 id 行 ──
        step_end = len(lines)
        for j in range(start + 1, len(lines)):
            if re.match(r"^\s*-\s*id:\s*\S+", lines[j]):
                step_end = j
                break

        # ★ 新规范优先：改 `target:`（单值）。
        #   旧 YAML 可能只有 `do:` 块 —— 那种情况回退到老逻辑，见下面。
        target_at = None
        for j in range(start + 1, step_end):
            if re.match(r"^(\s*)target:\s*", lines[j]):
                target_at = j
                break

        new_target = None
        for loc in ch["actual"]:
            new_target = _yaml_target_line(loc)
            if new_target:
                break

        if target_at is not None and new_target:
            indent = re.match(r"^(\s*)target:\s*", lines[target_at]).group(1)
            new_line = f"{indent}target: {new_target}\n"
            if lines[target_at] == new_line:
                continue
            lines[target_at] = new_line
            changed.append({"step": sid, "declared": ch["declared"],
                            "actual": ch["actual"], "field": "target"})
            continue

        # ── 回退：旧格式的 `do:` 块 ──
        new_items = [x for x in (_yaml_tap_line(l) for l in ch["actual"]) if x]
        if not new_items:
            skipped.append({"step": sid, "why": "实际动作无法表达成 YAML"})
            continue

        do_at = None
        for j in range(start + 1, step_end):
            if re.match(r"^(\s*)do:\s*$", lines[j]):
                do_at = j
                break
        if do_at is None:
            skipped.append({"step": sid,
                            "why": "该步既没有 `target:` 也没有 `do:` 块，"
                                   "自动插入风险高，留给人工"})
            continue

        indent = re.match(r"^(\s*)do:\s*$", lines[do_at]).group(1)
        item_indent = indent + "  "
        # do: 块的范围：其后所有缩进 >= item_indent 的行
        end = do_at + 1
        while end < len(lines):
            ln = lines[end]
            if not ln.strip():
                end += 1
                continue
            cur = len(ln) - len(ln.lstrip())
            if cur < len(item_indent):
                break
            end += 1

        old_block = "".join(lines[do_at + 1:end])
        new_block = "".join(f"{item_indent}{x}\n" for x in new_items)
        if old_block == new_block:
            continue
        lines[do_at + 1:end] = [new_block]
        changed.append({"step": sid, "declared": ch["declared"],
                        "actual": ch["actual"], "field": "do"})

    if changed and not dry_run:
        p.write_text("".join(lines), encoding="utf-8")

    return {"changed": changed, "skipped": skipped, "path": str(p),
            "dry_run": dry_run}


def render_change_lines(change: dict[str, Any], name: str = "") -> list[str]:
    """把一条差异渲染成人能读的短文本（进报告用）。"""
    sid = change["step"]
    old = " → ".join(change.get("declared") or []) or "(空)"
    new = " → ".join(change.get("actual") or []) or "(空)"
    head = f"{sid}（{name}）" if name else sid
    return [f"{head}：YAML 原写 {old}", f"{' ' * len(head)} 实际走 {new}"]
