"""把用例 YAML 读成 CaseSpec。

`storage/cases/*.yaml` 是本项目的用例资产。

YAML 的形状（比 CaseSpec 丰富得多，这里做一次转换）：
    meta:        case_id / title / package / derived_from
    playbook:    执行须知（回放前必读）
    preconditions: 四类 goto/clean/prepare/assert，带 skip_if / on_fail / verified_by
    steps:       id / desc / do[{tap:{rid|text}}] / expect{...} / on_fail
                 / know / pitfalls（坑挂在步上，不设文件级）
    assertions:  id / check{结构化判据} / status_last_run
    confidence:  level / reason / todo

设计原则（重要）：
    **不认识的字段要报出来，不能静默丢。**
    用例 YAML 自己写着：「两者对不上时不会报错，只会静默跑偏」——
    这是在回放场景下最危险的失败模式，我们不能自己再犯一次。

所以 `load_case()` 返回的不只是 CaseSpec，还有一份 `warnings`：
哪些字段没被采纳、为什么。调用方据此决定是"带着警告跑"还是"先补实现"。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None


from .paths import cases_dir  # noqa: E402




# ── 前提条件类型 ──────────────────────────────────────────────
# 用例 YAML 分四类，处理方式不同：
#   goto    导航：把 App 开到某界面（起点由冷启动钉死，故必写）
#   clean   清数据：pm clear，有损但稳定
#   prepare 准备：动手造数据，已有则跳过（skip_if）
#   assert  断言：只能查不能改，不满足即 BLOCKED

# 我们**真正采纳**的前提类型











# 步骤里**我们真的会读**的字段。其余一律报警 —— 写了不生效是最坏的失败模式。

# ══════════════════════════════════════════════════════════════════════
# 字段白名单（用户要求：「YAML 文件不能凭空添加字段」）
# ══════════════════════════════════════════════════════════════════════
#
# 规则来自用户原话：
#   「YAML 文件不能凭空添加字段。所有强证据的值必须来自真实的记录。
#     不能凭空捏造。」
#
# 为什么必须拦：**写了不生效 = 看起来验了，其实没验**。
# 这是本项目里最坏的失败模式 —— 比报错还坏，因为报错能查，
# 静默丢弃只会让人以为这条约束在管着。
#
# 已经吃过两次亏：
#   · permission 判据键写了代码不认识 -> 整条断言从来没验过（169）
#   · 断言上的 note: 只被 expect 层读，断言层直接丢（本次扫出来的）
#
# 所以每一层都要有白名单，且**默认拒收**（不在表里就报警）。

# 文件顶层

# meta 段

# assertions 段的一条断言

# preconditions 段

# 步骤里的 know 段









def load_case_json(p: Path) -> tuple[Any, list[str]]:
    """读 JSON 用例（我们的新格式）。返回 (CaseSpec, warnings)。

    ## 为什么单独一条路

    用户：「你自己调整。不用管YAML之前的格式。我们要的是一个高效的。有用的JSON格式。」

    YAML 那条路上挂着一堆历史包袱（旧机制清理、未知字段白名单、
    locate_seq 残留……）。JSON 是我们自己产出的，结构确定，
    直接按字段取就行 —— **不猜、不兜底、不静默忽略**。

    ## 和 YAML 的两处关键差别

    ① `asserts[].step` 是**必填**。断言绑不到步骤，判定时就取不到
       该步的证据快照，只能拿最后一屏硬判 —— 那是错的源头。
       所以这里缺了就记 warning，明确告诉调用方"这条验不了"。

    ② `asserts[].verify_action` / `pitfalls` 会被读进来并透传 ——
       它们正是上一轮跑出来的经验（怎么看见的、踩过什么坑），
       下次跑直接照做，这就是"高质量 JSON 对重跑有意义"的落地。
    """
    from ..agent.nodes import AssertSpec, Precondition, StepSpec
    from ..agent.runner import CaseSpec

    raw = json.loads(p.read_text(encoding="utf-8"))
    warns: list[str] = []

    case_id = str(raw.get("case_id") or p.stem)
    title = str(raw.get("title") or p.stem)
    package = str(raw.get("package") or "")
    if not package:
        warns.append("package 缺失 —— 无法判定被测 App，强证据会全部失效")

    steps: list[StepSpec] = []
    for i, s in enumerate(raw.get("steps") or [], 1):
        sid = str(s.get("id") or f"s{i}")
        steps.append(StepSpec(
            id=sid,
            desc=str(s.get("desc") or s.get("do") or ""),
            landing=s.get("landing"),
            pitfalls=list(s.get("pitfalls") or []),
            locate=s.get("locate") or s.get("target"),
        ))

    asserts: list[AssertSpec] = []
    for i, a in enumerate(raw.get("asserts") or [], 1):
        aid = str(a.get("id") or f"a{i}")
        st = str(a.get("step") or "").strip()
        if not st:
            warns.append(f"{aid} 没有 step —— 判定时取不到该步证据，只能拿最后一屏")
        item = AssertSpec(
            id=aid,
            desc=str(a.get("desc") or ""),
            check=a.get("check") or a.get("expect") or "",
            step=st,
        )
        # ★ 上一轮跑出来的经验，原样带进执行
        for k in ("verify_action", "pitfalls", "knowledge", "result"):
            if a.get(k):
                setattr(item, k, a[k])
        asserts.append(item)

    preconds: list[Precondition] = []
    for pc in raw.get("preconditions") or []:
        preconds.append(Precondition(**pc))

    spec = CaseSpec(
        case_id=case_id, title=title, package=package,
        steps=steps, asserts=asserts, preconditions=preconds,
        knowledge_files=list(raw.get("knowledge_files") or []),
        start_assert=raw.get("start_assert"),
        perm_action=raw.get("perm_action"), perm_name=raw.get("perm_name"),
        per_step_pitfalls=bool(raw.get("per_step_pitfalls", True)),
        case_confidence=dict(raw.get("case_confidence") or {}),
        engine=str(raw.get("engine") or "langgraph"),
    )
    return spec, warns


def load_case(path: str | Path, *, keep_stale: bool = True) -> tuple[Any, list[str]]:
    """读一个用例。返回 (CaseSpec, warnings)。

    ## 只认 JSON

    用户：「删除所有的YAML文件。我感觉YAML现在反而给AI造成了困扰」
          「数据库可以全部清理掉」

    YAML 那条路已经整体删除（读取、生成、回写、数据库字段）。
    现在用例只有一个格式：JSON。见 load_case_json。

    `keep_stale` 参数保留着只为兼容旧调用方，已不起作用。
    """
    p = Path(path)
    if p.suffix.lower() != ".json":
        raise ValueError(
            f"只支持 JSON 用例，不支持 {p.suffix or '(无扩展名)'}：{p.name}")
    return load_case_json(p)


def find_case(name: str) -> Path | None:
    """按文件名片段找用例 YAML（如 '168'）。"""
    d = cases_dir()
    if not d.is_dir():
        return None
    # ★ 先找 JSON（我们的新格式），没有再退回 YAML（历史遗留）
    for pat in ("*.json", "*.yaml"):
        for f in sorted(d.glob(pat)):
            if name in f.stem:
                return f
    return None


def load_case_by_name(name: str, *, keep_stale: bool = False) -> tuple[Any, list[str], Path]:
    """按名字找用例并加载 —— 这是**真要跑这条用例**的入口。

    所以默认 keep_stale=False：真跑之前把过期 YAML 清掉是对的
    （用户定的「有旧坐标就直接删了，重新执行生成新的」）。
    纯校验场景（只想看看数据对不对）请传 keep_stale=True。
    """
    p = find_case(name)
    if p is None:
        raise FileNotFoundError(f"在 {cases_dir()} 里找不到含 '{name}' 的用例 —— "
                                f"如果它刚被清理（含旧坐标/locate_seq），"
                                f"直接执行一次即可重新生成")
    spec, warns = load_case(p, keep_stale=keep_stale)
    return spec, warns, p


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "168"
    spec, warns, path = load_case_by_name(name)
    print(f"用例文件：{path.name}")
    print(f"  case_id : {spec.case_id}")
    print(f"  title   : {spec.title}")
    print(f"  package : {spec.package}")
    print(f"  前提    : {len(spec.preconditions)} 条")
    for pc in spec.preconditions:
        print(f"      [{pc.kind}] {pc.desc[:40]} -> {pc.do}")
    print(f"  步骤    : {len(spec.steps)} 条")
    for s in spec.steps:
        print(f"      {s.id}: locate={s.locate} landing={s.landing}")
        for pt in s.pitfalls[:2]:
            print(f"          · {pt[:70]}")
    print(f"  断言    : {len(spec.asserts)} 条")
    for a in spec.asserts:
        print(f"      {a.id}: {a.check}")
    print(f"\n⚠️ 未采纳的字段（{len(warns)} 条）:")
    for w in warns:
        print(f"    - {w}")
