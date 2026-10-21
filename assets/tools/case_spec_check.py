"""用例 YAML 格式检查器 —— 保证 YAML 里没有"写了却不生效"的东西。

──── 白名单从哪来（这是关键）────────────────────────────
**从当前工具实现倒推，不是从旧 YAML 迁就出来的。**

三条权威来源（改这里之前先去核对它们）：

  1. 数据模型 `child_agent.py`
       StepSpec      : id / desc / landing / pitfalls / locate / locate_seq
       AssertSpec    : id / desc / check
       Precondition  : kind(data|env|nav) / desc / do / undo / skip_if /
                       assert_start / target / hint
     ⇒ 这才是工具**真正要的字段**。

  2. 判据白名单 `child_agent.py: KNOWN_CHECKS`
       activity, activity_changed_to, package,
       node_present, node_absent, rid_present,
       node_text, text_present,
       dialog_present, dialog_present_on_tap, dialog_on_tap,
       nodes_present, after_tap, permission
     ⇒ `evaluate_check()` 只认这些，其余会打「不认识的判据键」。

  3. 落点表达式 `child_agent.py: match_landing`
       activity= / rid= / text= / desc= / package=
       逗号=AND，竖线=OR

──── 为什么强调"不迁就旧 YAML"──────────────────────────
旧 YAML 用的是 `do: [{tap: ...}]` + `expect: {...}` 这套写法，
那是**旧 SKILLS 的格式**，靠 `case_yaml.py` 事后翻译成上面的模型字段。
翻译环节每多一层就多一处静默丢失（`back: true` 被吞、`dialog_present`
没 rid 被丢…）。所以在写入时就直接用工具字段，是最短路径。

用法
    python assets\\tools\\case_spec_check.py                 # 全部
    python assets\\tools\\case_spec_check.py --case 168      # 按名字筛
    python assets\\tools\\case_spec_check.py --file x.yaml   # 指定文件
    python assets\\tools\\case_spec_check.py --strict        # warning 也算失败

退出码：0 = 可以落盘，1 = 不许落盘
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Any

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                 errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                 errors="replace")
except (AttributeError, ValueError):
    pass

CASES_DIR = Path(__file__).resolve().parent.parent / "storage" / "cases"


# ══════════════════════════════════════════════════════════
# 白名单
# ══════════════════════════════════════════════════════════

# 顶层只认这四个：正好对应 CaseSpec(SubagentTask) 的字段分组
TOP_KEYS = {"meta", "preconditions", "steps", "assertions"}
TOP_REQUIRED = {"meta", "steps", "assertions"}

META_KEYS = {"case_id", "title", "package", "knowledge_files", "perm_action",
             "perm_name"}
META_REQUIRED = {"case_id", "title", "package"}

# Precondition.kind 只有这三种（不是旧 YAML 的 clean/assert/goto）
PRECOND_KINDS = {"data", "env", "nav"}
PRECOND_KEYS = {"kind", "desc", "do", "undo", "skip_if", "assert_start",
                "target", "hint"}
SKIP_IF_KEYS = {"activity", "package", "pkg"}

# StepSpec
STEP_KEYS = {"id", "desc", "landing", "pitfalls", "locate", "locate_seq"}
STEP_REQUIRED = {"id", "desc"}

# AssertSpec
ASSERT_KEYS = {"id", "desc", "check"}
ASSERT_REQUIRED = {"id", "desc", "check"}

# 判据白名单 —— 逐字抄自 child_agent.KNOWN_CHECKS
KNOWN_CHECKS = {
    "activity", "activity_changed_to", "package",
    "node_present", "node_absent", "rid_present",
    "node_text", "text_present",
    "dialog_present", "dialog_present_on_tap", "dialog_on_tap",
    "nodes_present", "after_tap", "permission",
}
# 注解类：不参与判定，但允许出现（抄自 ANNOTATION_CHECKS）
ANNOTATION_CHECKS = {
    "note", "measured", "supporting", "computed", "placeholder",
    "reason", "evidence", "case_expect", "desc",
}

# 落点表达式里允许的 key（match_landing）
LANDING_KEYS = {"activity", "rid", "text", "desc", "package"}


class Report:
    """三档严重性。

    为什么分三档 —— 之前把两类完全不同的东西都塞进 warn，
    结果判断力全丢：
      · 死字段（写了绝不生效）：是**危害**，必须拦
      · 缺 do / 缺 landing：是**合法状态**（只观察的步），只是提示
    混在一起的后果：要么全放过（死字段偷渡），要么 --strict 全拦（误杀合法步骤）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []      # 硬错误
        self.dead: list[str] = []        # 写了但不生效
        self.warns: list[str] = []       # 提示

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def dead_field(self, msg: str) -> None:
        self.dead.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.dead


# ── 落点表达式检查 ────────────────────────────────────────

def _check_landing_expr(rep: Report, where: str, expr: Any,
                        *, required: bool) -> None:
    """landing / locate 是 `activity=X,rid=Y` 或 `rid=a|rid=b` 这种字符串。"""
    if expr is None:
        if required:
            rep.warn(f"{where} 没有判据 —— 代码无法判断，会交给模型")
        return
    if not isinstance(expr, str):
        rep.err(f"{where} 必须是字符串（如 \"activity=.Foo,rid=bar\"），"
                f"实际是 {type(expr).__name__}")
        return
    s = expr.strip()
    if not s:
        rep.err(f"{where} 是空字符串")
        return
    # 按 OR 拆，再按 AND 拆
    for or_part in s.split("|"):
        or_part = or_part.strip()
        if not or_part:
            rep.err(f"{where} 里有空的 OR 分支：{expr!r}")
            continue
        for and_part in or_part.split(","):
            and_part = and_part.strip()
            if not and_part:
                rep.err(f"{where} 里有空的 AND 分支：{expr!r}")
                continue
            if "=" not in and_part:
                rep.err(
                    f"{where} 的 `{and_part}` 缺少 `=` —— "
                    "落点表达式必须是 key=value，如 activity=.Foo")
                continue
            k = and_part.split("=", 1)[0].strip()
            if k not in LANDING_KEYS:
                rep.err(
                    f"{where} 用了不支持的落点键 `{k}`\n"
                    f"      → match_landing 只认：{', '.join(sorted(LANDING_KEYS))}")


# ── 结构化判据检查 ────────────────────────────────────────

def _check_need_rid(rep: Report, where: str, key: str, val: Any) -> None:
    """node_present / dialog_present 这类必须能取到 rid。

    曾经的 bug：对字符串直接 `if not rid` 判断，非空字符串一律放行，
    于是 `dialog_present: "关闭"` 漏检 —— 加载器会把 "关闭" 当 rid，
    变成**永远命不中**的判据。看着写了、其实恒假，比没写更糟。
    """
    if key == "nodes_present":
        if not isinstance(val, list):
            rep.err(f"{where}.{key} 必须是列表：[{{rid: a}}, {{rid: b}}]")
            return
        for i, one in enumerate(val):
            rid = one.get("rid") if isinstance(one, dict) else one
            if not rid:
                rep.err(f"{where}.{key}[{i}] 取不到 rid —— 该判据不会生效")
        return

    if isinstance(val, str):
        if key == "rid_present":
            if not val.strip():
                rep.err(f"{where}.{key} 是空字符串")
            return
        rep.err(
            f"{where}.{key}={val!r} 是裸字符串 —— 应写成 {{{key}: {{rid: xxx}}}}\n"
            f"      → 否则加载器会把 {val!r} 直接当 rid，成为永远命不中的判据")
        return

    rid = val.get("rid") if isinstance(val, dict) else None
    if not rid:
        rep.err(f"{where}.{key}={val!r} 里没有 rid —— 该判据不会生效")


def _check_check_dict(rep: Report, where: str, d: dict) -> None:
    for k, v in d.items():
        if k in ANNOTATION_CHECKS:
            continue
        if k not in KNOWN_CHECKS:
            rep.err(
                f"{where}.{k} 不是已实现的判据键\n"
                f"      → evaluate_check 只认：{', '.join(sorted(KNOWN_CHECKS))}")
            continue

        if k in ("node_present", "rid_present", "dialog_present",
                 "dialog_present_on_tap", "dialog_on_tap", "nodes_present"):
            _check_need_rid(rep, where, k, v)

        elif k == "node_text":
            if not isinstance(v, dict):
                rep.err(f"{where}.node_text 必须是 {{rid: x, equals: \"文案\"}}")
            elif not v.get("rid"):
                rep.err(f"{where}.node_text 里没有 rid —— 不会生效")

        elif k == "text_present":
            if not isinstance(v, str) or not v.strip():
                rep.err(f"{where}.text_present 必须是非空字符串")

        elif k == "node_absent":
            if isinstance(v, dict) and not v.get("rid"):
                rep.err(f"{where}.node_absent 里没有 rid")
            elif isinstance(v, str) and not v.strip():
                rep.err(f"{where}.node_absent 是空字符串")

        elif k in ("activity", "activity_changed_to"):
            if not isinstance(v, str) or not v.strip():
                rep.err(f"{where}.{k} 必须是非空字符串（activity 类名）")

        elif k == "package":
            if not isinstance(v, str) or not v.strip():
                rep.err(f"{where}.package 必须是非空字符串（包名）")

        elif k == "permission":
            if not isinstance(v, dict) or not v:
                rep.err(
                    f"{where}.permission 必须是 {{CAMERA: true}} 这种形状"
                    "（判的是被测 App 的授权状态）")

        elif k == "after_tap":
            if not isinstance(v, dict):
                rep.err(f"{where}.after_tap 必须是字典（内部是子判据）")
            else:
                _check_check_dict(rep, f"{where}.after_tap", v)


def _check_check(rep: Report, where: str, check: Any) -> None:
    if check is None:
        return
    if isinstance(check, str):
        _check_landing_expr(rep, where, check, required=False)
        return
    if not isinstance(check, dict):
        rep.err(f"{where} 必须是字符串或字典，实际是 {type(check).__name__}")
        return
    if not check:
        rep.err(f"{where} 是空字典 —— 等于没判")
        return
    _check_check_dict(rep, where, check)


# ── pitfalls 检查 ─────────────────────────────────────────

def _check_pitfalls(rep: Report, where: str, pf: Any) -> None:
    """`StepSpec.pitfalls` 是 `list[str]` —— 每条坑一个字符串。

    为什么这个字段值得认真校验：
    它是**按步喂给模型**的（child_agent:1588 把 `step["pitfalls"]` 直接塞进
    decide 的 payload），走到哪步就看那步的坑。所以：

      · 写成字符串 -> 模型真能读到
      · 写成 dict（旧格式 {text, from, quote_hash}）-> 类型不对。
        `list[str]` 只做**静态类型标注**，Pydantic 默认不强制转换，
        所以要在这里拦 —— 否则模型拿到的是一个 dict，读不出人写的那句话。

    旧 YAML 的 `{text: ..., from: ..., quote_hash: ...}` 是为了给
    `kb_check.py` 校验引用锚用的。但**引用锚属于知识卡的维护职责**，
    不该塞进用例的步骤坑里 —— 步骤坑只需要"走到这步要知道什么"。
    """
    if not isinstance(pf, list):
        rep.err(f"{where}.pitfalls 必须是列表（每条坑一个字符串）")
        return
    for i, one in enumerate(pf):
        if isinstance(one, dict):
            has_text = bool(str(one.get("text") or "").strip())
            rep.dead_field(
                f"{where}.pitfalls[{i}] 是字典 —— StepSpec.pitfalls 是 list[str]，"
                "模型读不到\n"
                "      → 改成纯字符串；"
                + ("该条内容在 .text 里，搬出来即可"
                   if has_text else "但里面连 text 都没有"))
            continue
        if not isinstance(one, str):
            rep.err(f"{where}.pitfalls[{i}] 必须是字符串，"
                    f"实际是 {type(one).__name__}")
            continue
        if not one.strip():
            rep.warn(f"{where}.pitfalls[{i}] 是空字符串")


# ── 主体 ─────────────────────────────────────────────────

def check_file(path: Path) -> Report:
    rep = Report(path)
    try:
        import yaml
    except ImportError:
        rep.err("需要 PyYAML：pip install pyyaml")
        return rep

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        rep.err(f"文件不存在：{path}")
        return rep
    except Exception as e:  # noqa: BLE001
        rep.err(f"YAML 无法解析：{e}")
        return rep

    if raw is None:
        rep.err("文件是空的（只有注释？）")
        return rep
    if not isinstance(raw, dict):
        rep.err(f"顶层必须是字典，实际是 {type(raw).__name__}")
        return rep

    # ── 顶层 ──
    for k in raw:
        if k not in TOP_KEYS:
            rep.dead_field(
                f"顶层键 `{k}` 不认识 —— 不会生效"
                f"（只认 {', '.join(sorted(TOP_KEYS))}）")
    for k in TOP_REQUIRED - set(raw):
        rep.err(f"缺少顶层段 `{k}` —— 必填")

    # ── meta ──
    meta = raw.get("meta")
    if not isinstance(meta, dict):
        rep.err("meta 必须是字典")
        meta = {}
    for k in META_REQUIRED:
        if not meta.get(k):
            rep.err(f"meta.{k} 缺失或为空 —— 必填")
    for k in meta:
        if k in ANNOTATION_CHECKS:
            continue
        if k not in META_KEYS:
            rep.dead_field(f"meta.{k} 不认识 —— 不会生效")

    # ── preconditions ──
    pcs = raw.get("preconditions")
    if pcs is not None:
        if not isinstance(pcs, list):
            rep.err("preconditions 必须是列表")
        else:
            for i, pc in enumerate(pcs):
                w = f"preconditions[{i}]"
                if not isinstance(pc, dict):
                    rep.err(f"{w} 必须是字典")
                    continue
                kind = pc.get("kind")
                if not kind:
                    rep.err(f"{w} 缺少 kind —— 必填（data / env / nav）")
                elif kind not in PRECOND_KINDS:
                    rep.err(
                        f"{w}.kind=`{kind}` 不支持\n"
                        f"      → Precondition.kind 只有 "
                        f"{', '.join(sorted(PRECOND_KINDS))}"
                        "（clean/assert/goto 是旧格式，需要 case_yaml 翻译）")
                if kind == "env" and not pc.get("undo"):
                    # 模型层 model_validator 会直接抛错
                    rep.err(f"{w} 是 env 类但没给 undo —— "
                            "会污染下一条用例，模型层会直接拒绝")
                if kind == "nav" and not pc.get("target"):
                    rep.warn(f"{w} 是 nav 但没给 target（要去哪个界面）")
                for k in pc:
                    if k in ANNOTATION_CHECKS:
                        continue
                    if k not in PRECOND_KEYS:
                        rep.dead_field(f"{w}.{k} 不认识 —— 不会生效")
                sk = pc.get("skip_if")
                if isinstance(sk, dict):
                    bad = set(sk) - SKIP_IF_KEYS
                    if bad:
                        rep.warn(
                            f"{w}.skip_if 含判不了的键 {sorted(bad)} —— 会保守照跑")

    # ── steps ──
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        rep.err("steps 必须是非空列表")
        steps = []
    seen: set[str] = set()
    for i, s in enumerate(steps):
        w = f"steps[{i}]"
        if not isinstance(s, dict):
            rep.err(f"{w} 必须是字典")
            continue
        sid = s.get("id")
        if not sid:
            rep.err(f"{w} 缺少 id —— 必填")
        else:
            w = f"step {sid}"
            if sid in seen:
                rep.err(f"{w}: id 重复")
            seen.add(sid)
        for k in STEP_REQUIRED - {"id"}:
            if not s.get(k):
                rep.err(f"{w} 缺少 {k} —— 必填")
        for k in s:
            if k in ANNOTATION_CHECKS:
                continue
            if k not in STEP_KEYS:
                rep.dead_field(
                    f"{w}.{k} 不认识 —— 不会生效\n"
                    f"      → StepSpec 只有 {', '.join(sorted(STEP_KEYS))}"
                    "（do/expect 是旧格式，需翻译；直接写 locate/landing 更短）")
        _check_landing_expr(rep, f"{w}.landing", s.get("landing"),
                            required=True)
        _check_landing_expr(rep, f"{w}.locate", s.get("locate"),
                            required=False)
        ls = s.get("locate_seq")
        if ls is not None:
            if not isinstance(ls, list):
                rep.err(f"{w}.locate_seq 必须是列表")
            else:
                for j, one in enumerate(ls):
                    _check_landing_expr(rep, f"{w}.locate_seq[{j}]", one,
                                        required=True)
        pf = s.get("pitfalls")
        if pf is not None:
            _check_pitfalls(rep, w, pf)

    # ── assertions ──
    asserts = raw.get("assertions")
    if not isinstance(asserts, list) or not asserts:
        rep.err("assertions 必须是非空列表 —— 至少要有一条验收标准")
        asserts = []
    aids: set[str] = set()
    for i, a in enumerate(asserts):
        w = f"assertions[{i}]"
        if not isinstance(a, dict):
            rep.err(f"{w} 必须是字典")
            continue
        aid = a.get("id")
        if aid:
            w = f"assert {aid}"
            if aid in aids:
                rep.err(f"{w}: id 重复")
            aids.add(aid)
        for k in ASSERT_REQUIRED:
            if not a.get(k):
                rep.err(f"{w} 缺少 {k} —— 必填")
        for k in a:
            if k in ANNOTATION_CHECKS:
                continue
            if k not in ASSERT_KEYS:
                rep.dead_field(
                    f"{w}.{k} 不认识 —— 不会生效"
                    f"（AssertSpec 只有 {', '.join(sorted(ASSERT_KEYS))}）")
        _check_check(rep, f"{w}.check", a.get("check"))

    return rep


def _is_scratch(p: Path) -> bool:
    """测试夹具 / 临时文件：文件名以 `_` 开头，或藏在 `_archive/` 下。

    为什么要跳过：检查器默认扫 `cases/*.yaml`，而 `_test_invalid.yaml`
    这类故意写错的夹具也会被扫到，导致「全量检查」永远报失败。
    """
    return p.name.startswith("_") or p.parent.name.startswith("_")


def self_consistency() -> tuple[list[str], bool]:
    """检查「本文件的白名单」与「加载器/模型实际能力」是否对得上。

    返回 (problems, ran)：
      · problems —— 对不上的地方
      · **ran** —— 探针到底有没有真的跑起来。⚠️ 必须区分「通过」和「没跑」，
        因为两者都会让 problems 为空。上一轮的教训就是：
        `case_yaml` 不读字段、检查器仍判 ✅ —— 沉默被当成了正确。

    为什么必须有这一节（真踩过的坑）：
    按 SPEC 直接写 `locate` / `locate_seq` / `landing` 的 YAML，
    曾经**通过格式检查、却在加载器里被完全忽略** ——
    因为 `case_yaml.py` 当时只从 `expect:` / `do:` 里取。
    检查器验的是"符合我写的规范"，不是"实际能不能被读"，
    于是规范文档和实现各说各的，还都显示 ✅。

    ⇒ 这里直接 import 真实模块做探针，对不上就报错。
    """
    problems: list[str] = []
    ran = True          # 至少有一道探针真的执行过，才置 True
    # 路径：真实模块在 src/android_agent/ 下，而本脚本在 assets/tools/ ——
    # 两处都要进 sys.path。
    _tools = Path(__file__).resolve().parent
    _assets = _tools.parent
    _root = _assets.parent
    for p in (_tools, _assets, _root):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    try:
        from child_agent import KNOWN_CHECKS as REAL_CHECKS
        from child_agent import Precondition, StepSpec
    except Exception as e:  # noqa: BLE001
        # ⚠️ 不要在这里 `return` —— 那会连带跳过下面最关键的加载器探针（④），
        #    而 ④ 根本不依赖 child_agent。一个模块坏了不该拖垮别的防线。
        REAL_CHECKS = None
        StepSpec = None          # type: ignore[assignment]
        Precondition = None      # type: ignore[assignment]
        problems.append(f"无法导入执行器模块（child_agent）：{e}\n"
                        "      → 判据/字段白名单无法核对（但仍会验加载器）")

    if REAL_CHECKS is not None:
        ran = True
        # ① 判据白名单
        missing = set(REAL_CHECKS) - KNOWN_CHECKS
        extra = KNOWN_CHECKS - set(REAL_CHECKS)
        if missing:
            problems.append(
                f"KNOWN_CHECKS 里少了执行器已实现的判据：{sorted(missing)}\n"
                "      → 这些判据写了能生效，但会被本检查器误判成 ERROR")
        if extra:
            problems.append(
                f"KNOWN_CHECKS 里有执行器并不认识的判据：{sorted(extra)}\n"
                "      → 这些写进 YAML 会被执行器忽略，检查器却放行")

    # ② StepSpec 字段
    if StepSpec is not None:
        real_step = set(StepSpec.model_fields)
        if STEP_KEYS != real_step:
            problems.append(
                f"STEP_KEYS 与 StepSpec 不一致：\n"
                f"      检查器多的：{sorted(STEP_KEYS - real_step)}\n"
                f"      检查器少的：{sorted(real_step - STEP_KEYS)}")

    # ③ Precondition.kind
    try:
        if Precondition is None:
            raise RuntimeError("child_agent 未导入")
        import typing
        kinds = set(typing.get_args(
            Precondition.model_fields["kind"].annotation))
        if kinds and kinds != PRECOND_KINDS:
            problems.append(
                f"PRECOND_KINDS 与 Precondition.kind 不一致："
                f"检查器 {sorted(PRECOND_KINDS)} vs 模型 {sorted(kinds)}")
    except Exception:  # noqa: BLE001
        pass

    # ④ 加载器要真的能读「直接字段」（这是最要命的一条）
    #
    # 真实事故：按 SPEC 直接写 locate/landing 的 YAML，能通过格式检查，
    # 却在 case_yaml.load_case 里被完全忽略（当时只认旧格式 do:/expect:），
    # 读出来 locate=None、landing=None —— 等于一条判据都没有。
    # 检查器当时还给了 ✅，因为它验的是"符合我写的规范"，不是"实际能不能读"。
    try:
        import case_yaml
        import tempfile
        src = (
            "meta: {case_id: 'probe', title: probe, package: com.probe}\n"
            "preconditions:\n"
            "  - kind: data\n"
            "    desc: probe\n"
            "    do: echo probe\n"
            "  - kind: nav\n"
            "    desc: probe-nav\n"
            "    target: some page\n"
            "    assert_start: activity=.Probe\n"
            "steps:\n"
            "  - id: s0\n"
            "    desc: probe0\n"
            "    locate_seq: [rid=probe_a, rid=probe_b]\n"
            "    landing: activity=.Probe\n"
            "  - id: s1\n"
            "    desc: probe1\n"
            "    locate: rid=probe_c\n"
            "    landing: activity=.Probe\n"
            "assertions:\n"
            "  - id: a1\n"
            "    desc: probe\n"
            "    check: {activity: .Probe}\n"
        )
        with tempfile.NamedTemporaryFile(
                "w", suffix=".yaml", encoding="utf-8", delete=False) as f:
            f.write(src)
            tmp = f.name
        try:
            spec, _warns = case_yaml.load_case(tmp)
            ran = True          # ★ 走到这里说明加载器探针真的执行了

            # ④-a 前提：kind 必须被采纳
            if len(spec.preconditions) != 2:
                problems.append(
                    f"加载器只采纳了 {len(spec.preconditions)} 条前提（期望 2 条）\n"
                    "      → 按 SPEC 用 `kind:` 写的前提可能被丢弃，"
                    "连带 assert_start 起点校验失效")
            else:
                navs = [p for p in spec.preconditions if p.kind == "nav"]
                if not navs:
                    problems.append(
                        "加载器没把 `kind: nav` 识别成 nav 前提\n"
                        "      → nav 前提会被当成普通 data 丢掉")
                elif not navs[0].assert_start:
                    problems.append(
                        "加载器丢了前提的 `assert_start`\n"
                        "      → 起点不会被校验（历史事故：168 报「预备清单为空」）")

            # ④-b 步骤：locate_seq / landing
            st0 = spec.steps[0]
            if list(st0.locate_seq or []) != ["rid=probe_a", "rid=probe_b"]:
                problems.append(
                    f"加载器没读对 step 的 `locate_seq`"
                    f"（实际 {st0.locate_seq!r}）")
            if st0.locate != "rid=probe_a":
                problems.append(
                    f"加载器没把 `locate_seq` 首项同步到 `locate`"
                    f"（实际 {st0.locate!r}）")
            st1 = spec.steps[1]
            if st1.locate != "rid=probe_c":
                problems.append(
                    f"加载器没读对 step 的 `locate` 直接字段"
                    f"（实际 {st1.locate!r}）")
            for st in (st0, st1):
                if st.landing != "activity=.Probe":
                    problems.append(
                        f"加载器没读对 {st.id} 的 `landing` 直接字段"
                        f"（实际 {st.landing!r}）\n"
                        "      → 按 SPEC 写的落点判据会静默失效")
        finally:
            Path(tmp).unlink(missing_ok=True)
    except ImportError as e:
        ran = False
        problems.append(f"无法导入 case_yaml，加载器探针没跑：{e}")
    except Exception as e:  # noqa: BLE001
        ran = False
        problems.append(f"加载器探针执行失败：{e}")

    return problems, ran


def main() -> int:
    ap = argparse.ArgumentParser(description="用例 YAML 格式检查")
    ap.add_argument("--case", help="按文件名片段筛选")
    ap.add_argument("--file", help="只检查指定文件（不受 _ 前缀跳过限制）")
    ap.add_argument("--all", action="store_true",
                    help="连 _ 开头的夹具文件也一起检查")
    ap.add_argument("--strict", action="store_true", help="warning 也算失败")
    ap.add_argument("--skip-consistency", action="store_true",
                    help="跳过「规范 vs 执行器」一致性自检（不建议）")
    args = ap.parse_args()

    if args.file:
        # 显式指定文件时**一律检查**，方便单独验证夹具
        files = [Path(args.file)]
    else:
        if not CASES_DIR.is_dir():
            print(f"目录不存在：{CASES_DIR}", file=sys.stderr)
            return 2
        files = sorted(CASES_DIR.glob("*.yaml"))
        if not args.all:
            skipped = [f.name for f in files if _is_scratch(f)]
            files = [f for f in files if not _is_scratch(f)]
            if skipped:
                print(f"（跳过 {len(skipped)} 个夹具/归档文件："
                      f"{', '.join(skipped)}）\n")
        if args.case:
            files = [f for f in files if args.case in f.stem]

    if not files:
        print("没有找到要检查的 YAML")
        return 0

    # ★ 先验「规范 vs 实现」是否一致 —— 不一致的话，
    #   下面所有 ✅ 都不可信（曾经就是这样把失效字段判成了合格）。
    #
    # ⚠️ 必须**打印一行「已执行」**，哪怕零问题。
    #   原来零问题时什么都不输出，于是「真的通过了」和「探针根本没跑」
    #   在终端上长得一模一样 —— 这和 `case_yaml` 不读字段却判 ✅
    #   是同一个失败模式：**沉默不等于正确**。
    problems, ran = self_consistency()
    if not ran:
        print("❌ 一致性自检**未能执行**（见下），下面的 ✅ 不能作为可信结论：")
        for p in problems:
            print(f"   {p}")
        if not args.skip_consistency:
            print("⇒ 先让自检能跑起来。\n")
            return 1
    elif problems:
        print("❌ 规范与执行器不一致（下面每条都要修，否则检查结果不可信）：")
        for p in problems:
            print(f"   {p}")
        print("⇒ 修完再跑。\n")
        if not args.skip_consistency:
            return 1
    else:
        print("✔ 一致性自检通过（判据白名单 / StepSpec 字段 / "
              "Precondition.kind / 加载器读取 均与执行器一致）\n")

    bad = 0
    n_err = n_dead = n_warn = 0
    for f in files:
        rep = check_file(f)
        n_err += len(rep.errors)
        n_dead += len(rep.dead)
        n_warn += len(rep.warns)
        failed = (not rep.ok) or (args.strict and rep.warns)
        if failed:
            bad += 1
            print(f"❌ {f.name}")
        elif rep.warns:
            print(f"⚠️  {f.name}")
        else:
            print(f"✅ {f.name}")
        for e in rep.errors:
            print(f"   ERROR   {e}")
        for d in rep.dead:
            print(f"   DEAD    {d}")
        for w in rep.warns:
            print(f"   warn    {w}")

    print(f"\n共 {len(files)} 个文件：{bad} 个不合格 | "
          f"{n_err} ERROR | {n_dead} 死字段 | {n_warn} warning")
    if bad:
        print("⇒ 不合格，不许落盘。修完重跑。")
        return 1
    print("⇒ 合格，可以落盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
