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

import re
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None

import yaml  # noqa: E402

from .paths import cases_dir  # noqa: E402


class StaleCaseYaml(Exception):
    """用例 YAML 含已废弃的机制（坐标 / locate_seq），且已被清理。

    单独一个异常类型而不是 FileNotFoundError：
      · 文件是被**主动删除**的，不是不存在
      · 调用方可以据此区分"路径写错了"和"该重新生成用例了"
    """


# ── 前提条件类型 ──────────────────────────────────────────────
# 用例 YAML 分四类，处理方式不同：
#   goto    导航：把 App 开到某界面（起点由冷启动钉死，故必写）
#   clean   清数据：pm clear，有损但稳定
#   prepare 准备：动手造数据，已有则跳过（skip_if）
#   assert  断言：只能查不能改，不满足即 BLOCKED
PRECOND_KINDS = ("goto", "clean", "prepare", "assert")

# 我们**真正采纳**的前提类型
PRECOND_SUPPORTED = ("clean",)          # 其余先记 warning


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _tap_to_locate(do: list[Any]) -> tuple[str | None, list[str], list[str]]:
    """从 `do:` 抽出可回放的定位/动作序列。

    支持的动作形态：
        {tap: {rid: x}}          -> "rid=x"
        {tap: {text: y}}         -> "text=y"
        {back: true}             -> "key=back"      系统返回键
        {home: true}             -> "key=home"      系统 Home 键
        {key: back}              -> "key=back"      `key:` 直接写键名

    返回 (首个 locate, 全部 locate 序列, warnings)。

    ⚠️ 用例 YAML 一步可能有**多个动作**（点"更多"再点"课程表"）。
    早先只取第一个，第二个就丢了 —— 而丢了它这一步根本走不完
    （点开菜单但不选项 = 什么都没发生）。所以现在**全部保留**，
    由执行器按顺序依次做。

    ⚠️ 教训（会话 #84 实测）：`back: true` 曾被**静默丢弃**。
    用例 169 的 s3 第一动作是「从相机按返回」，但解析层只认 `tap`，
    `back` 既不报错也不警告地消失了。执行器于是从相机页直接去找
    btnImportFromGallery —— 那当然找不到，模型只好在相机里乱点，
    连点 intent_back_button 十几轮，白烧 4 分钟。
    ⇒ **不认识的动作必须报 warning，绝不能静默丢**。
    """
    warns: list[str] = []
    seq: list[str] = []
    for act in do:
        if not isinstance(act, dict):
            warns.append(f"do 里出现非字典项，已跳过：{act!r}")
            continue
        if "tap" in act:
            t = act["tap"] or {}
            if not isinstance(t, dict):
                warns.append(f"tap 不是字典，已跳过：{t!r}")
                continue
            if t.get("rid"):
                seq.append(f"rid={t['rid']}")
            elif t.get("text"):
                seq.append(f"text={t['text']}")
            else:
                warns.append(f"tap 既无 rid 也无 text，已跳过：{t}")
            continue

        # 系统键：back / home 这类没有控件可定位，必须单独表达
        key = None
        for name in ("back", "home", "enter", "menu"):
            if name in act:
                v = act[name]
                if v is True or (isinstance(v, str) and v.strip()):
                    key = name
                break
        if key is None and isinstance(act.get("key"), str):
            key = act["key"].strip().lower()
        if key:
            seq.append(f"key={key}")
            continue

        if "swipe" in act or "input" in act:
            # 知道存在、但回放层目前表达不了 —— 明确说出来，
            # 交给模型执行，而不是假装它不存在
            warns.append(
                f"do 里的 `{next(iter(act))}` 动作暂不支持回放，"
                "已忽略该动作（建议该步直接交给模型）")
            continue
        warns.append(f"do 里的动作不认识，已跳过：{act}")

    if not seq:
        return None, [], warns
    return seq[0], seq, warns


def _expect_to_landing(expect: Any) -> tuple[str | None, list[str]]:
    """把 `expect:` 转成我们的 landing 字符串（--expect 语法）。

    expect 可能是：
        activity: ".Foo"                     -> activity=.Foo
        activity_changed_to: ".Foo"          -> activity=.Foo
        package: "com.x"                     -> 我们判不了（landing 无此语法）
        dialog_present: {rid: button1}       -> rid=button1
        node_present: {rid: x}               -> rid=x
        others...                            -> 报 warning
    """
    warns: list[str] = []
    if not expect:
        return None, warns
    if isinstance(expect, str):
        return expect.strip() or None, warns
    if not isinstance(expect, dict):
        return None, [f"expect 形状不认识：{type(expect).__name__}"]

    parts: list[str] = []
    for k, v in expect.items():
        if k in ("activity", "activity_changed_to"):
            parts.append(f"activity={v}")
        elif k in ("node_present", "rid_present", "dialog_present"):
            rid = v.get("rid") if isinstance(v, dict) else v
            if rid:
                parts.append(f"rid={rid}")
            else:
                # ★ 以前这里是**静默丢弃**：dict 里没有 rid 键就什么都不过问。
                # 169 的 a1 写的是
                #     dialog_present: {permission_dialog: "CAMERA"}
                # 没有 rid → 直接消失，**连一行警告都没有**。
                # 结果：那条断言从来没被执行过，而报告看起来"验过了"。
                # 静默丢弃比报错更糟 —— 它伪装成"没问题"。
                warns.append(
                    f"expect.{k}={v} 里没有 rid，无法转成判据（未参与判定）")
        elif k == "package":
            # ★ 包名是**唯一确定**的判据，不该被丢弃。
            # 169 的 s2/s3 就写了 `expect.package: com.zui.camera` ——
            # 意思明确："这一刻前台该是相机"。以前当"无法表达"跳过，
            # 那条预期等于从来没验过。
            # match_landing 用**相等**匹配（不是子串），避免 com.zui 误中
            # com.zui.calendar。
            if v:
                parts.append(f"package={v}")
        elif k == "no_dialog":
            # ⚠️ 这里**故意不转成判据**。
            # match_landing 只认 activity/rid/text/desc 四种正向条件，
            # 没有否定语法。硬写个 "!text=..." 只会让 key 变成 "!text"，
            # _one() 永远返回 False —— 判据反而变成"永远不命中"，
            # 把一个正确的前提验成失败。宁可不判，也不写错的判据。
            # 这类前提由 start_check + 模型的弹窗处理策略覆盖。
            warns.append(
                f"expect.no_dialog={v} 无法表达为落点判据（无否定语法），"
                "未参与起点校验；弹窗处理交给执行期策略")
        elif k == "note":
            pass                       # 注解，本就不该判
        elif k == "nodes_present":
            for one in _as_list(v):
                rid = one.get("rid") if isinstance(one, dict) else one
                if rid:
                    parts.append(f"rid={rid}")
        else:
            warns.append(f"expect.{k} 未实现（未参与判定）")
    return (",".join(parts) if parts else None), warns


def _precond_from(entry: dict[str, Any], i: int) -> tuple[Any | None, list[str]]:
    """把 YAML 的一条前提转成我们的 Precondition。

    ★ 两种写法都支持（直接字段优先）：

      ① 新规范（SPEC.md 推荐，直接用 Precondition.kind）：
           - kind: data
             do: pm clear com.zui.calendar
           - kind: nav
             target: 日历首页
             assert_start: activity=.AllInOneActivity
           - kind: env
             do: ...
             undo: ...

      ② 旧格式（type + 处理方式）：
           - type: clean / assert / goto / prepare

    ⚠️ 真实事故：曾经只认 ②，于是按 ① 写的 `kind: nav` 前提**被整个丢弃**，
    连带 `assert_start` 起点校验也没生效，报告里表现为「预备清单为空」。
    注意 `kind: data` **碰巧能用**（旧代码把它当默认值），
    所以这个 bug 只在 nav/env 上暴露 —— 更隐蔽。
    """
    from ..agent.nodes import Precondition

    warns: list[str] = []
    ptype = str(entry.get("type") or "").strip()
    nkind = str(entry.get("kind") or "").strip()
    desc = str(entry.get("desc") or f"前提{i+1}")

    # ── 新格式：直接给了 Precondition.kind ──
    if nkind:
        if nkind not in ("data", "env", "nav"):
            warns.append(f"前提 kind=`{nkind}` 不认识（只支持 data/env/nav）")

        undo: list[str] = []
        raw_undo = entry.get("undo")
        if isinstance(raw_undo, str) and raw_undo.strip():
            undo = [raw_undo.strip()]
        elif isinstance(raw_undo, list):
            undo = [str(u).strip() for u in raw_undo if str(u).strip()]

        # assert_start 可以是字符串（activity=.Foo）或字典（{activity: .Foo}）
        start_assert = None
        raw_as = entry.get("assert_start")
        if raw_as:
            if isinstance(raw_as, str):
                start_assert = raw_as.strip() or None
            else:
                start_assert, ew = _expect_to_landing(raw_as)
                warns += ew

        # nav 的前提里常把起点写进 assert_start；没有的话用 target 的提示
        hint = " ".join(str(entry.get(k) or "").strip()
                        for k in ("target", "hint")).strip()
        if nkind == "nav" and not hint and not start_assert:
            warns.append(f"前提 nav（{desc[:24]}）既没有 target/hint 也没有 "
                         "assert_start —— 无法指导模型走到起点")

        cmds: list[str] = []
        raw_do = entry.get("do")
        if isinstance(raw_do, str) and raw_do.strip():
            cmds = [raw_do.strip()]
        elif isinstance(raw_do, list):
            cmds = [str(c).strip() for c in raw_do if str(c).strip()]

        skip_if = entry.get("skip_if") if isinstance(entry.get("skip_if"), dict) \
            else None

        try:
            return Precondition(kind=nkind, desc=desc, do=cmds, undo=undo,
                                skip_if=skip_if, assert_start=start_assert,
                                target=str(entry.get("target") or ""),
                                hint=hint), warns
        except Exception as e:  # noqa: BLE001
            # 典型：env 没给 undo，模型层会抛
            return None, warns + [f"前提 `{desc[:24]}` 构造失败：{e}"]

    # ── 旧格式：type ──
    if ptype and ptype not in PRECOND_KINDS:
        warns.append(f"前提类型 `{ptype}` 不认识（按原样处理）")
    # ⚠️ 别再用"暂未实现分类处理"这种笼统说法 —— 它会把三种完全不同的情况
    #    混成一句，让人以为只是功能缺失，而真正该被看见的是"这条前提没生效"。
    #
    #    现在的三类各说各的：
    #      · assert  -> 已实现（转成起点校验 assert_start），不报警
    #      · goto    -> 已实现（转成 nav 起点要求，交给模型走过去），不报警
    #      · prepare -> **确实没有自动实现**：它的 do 常是中文操作描述，
    #                   执行器不会替模型造数据，只是把描述交给模型。
    #                   这一点必须**明说**，否则报告里看不出"状态没被准备"。
    if ptype == "prepare":
        warns.append(
            f"前提 `prepare`（{desc[:24]}）执行器不代劳造数据，"
            f"只把它交给模型按描述准备 —— 若状态未满足，相关步骤可能白走")

    raw_do = entry.get("do")
    cmds: list[str] = []
    if isinstance(raw_do, str) and raw_do.strip():
        cmds = [raw_do.strip()]
    elif isinstance(raw_do, list):
        cmds = [str(c).strip() for c in raw_do if str(c).strip()]

    # ══════════════════════════════════════════════════════════════════
    # ★ type=goto 必须在 `if not cmds` **之前**判。
    # ══════════════════════════════════════════════════════════════════
    #
    # 曾放在 `if not cmds:` 里面 —— 于是**写了 `do` 的 goto 就永远进不去**，
    # 掉到下面被当成普通 data 前提，nav 语义整个丢掉：
    # 起点判据没了、给模型的路径提示也没了，而这正是这个类型存在的理由。
    # 实测就是被 test_goto_nav 抓出来的：样例 goto 带一句 do 说明，
    # 结果 kind 是 data 而不是 nav。
    #
    # goto 的 `do` 本来就是**人话描述**（"点 iv_more 再点课程表"），
    # 不是 shell 命令，不该参与"有没有 do"的判断。
    if ptype == "goto":
        g_target = entry.get("target")
        g_assert = None
        if isinstance(g_target, dict):
            # 结构化 target：{"activity": ".Foo"} 这种，直接转判据
            g_assert, gw = _expect_to_landing(g_target)
            warns += gw
        elif isinstance(g_target, str):
            # target 形如「课程表页（.timetable.display.TimetableActivity）」
            # 或「日历首页（com.zui.calendar/.AllInOneActivity）」——
            # 把括号里的 activity 抠出来当判据，抠不到就只留提示。
            m = re.search(r"[（(]\s*(?:[\w.]+/)?(\.[\w.]+|[\w.]+(?:Activity|Launcher))\s*[）)]",
                          g_target)
            if m:
                g_assert = f"activity={m.group(1)}"
            elif "launcher" in g_target.lower() or "桌面" in g_target:
                # 桌面类：用 package 判据更稳（launcher 的 activity 名字各机型不同）
                g_assert = "package=com.zui.launcher"

        # 没从 target 拿到判据，再看有没有显式 assert_start / check
        if not g_assert:
            for k in ("assert_start", "check", "expect"):
                if entry.get(k):
                    g_assert, gw = _expect_to_landing(entry[k])
                    warns += gw
                    break

        skip_if = entry.get("skip_if")
        # ★ hint = "怎么走到起点"的提示，按价值从高到低取：
        #     path/hint（专为此写的） -> do（人话操作描述） -> target 字面量
        #
        # goto 的 `do` 本来就是人话（"点 iv_more 再点课程表"），
        # 拿它当路径提示，比丢掉强得多 —— 模型据此才知道该往哪走。
        # 早先只取 path，结构化 target 的样例于是 hint 为空，
        # 模型只拿到一个 activity 名字，等于没告诉它怎么去。
        hint = str(entry.get("path") or entry.get("hint") or "").strip()
        if not hint and isinstance(raw_do, str):
            hint = raw_do.strip()
        if not hint and isinstance(g_target, str):
            hint = g_target.strip()
        return Precondition(
            kind="nav", desc=desc, do=[],
            undo=[], skip_if=skip_if if isinstance(skip_if, dict) else None,
            assert_start=g_assert,
            target=str(g_target if not isinstance(g_target, dict)
                       else g_assert or "")[:200],
            hint=hint[:600],
        ), warns

    if not cmds:
        # 没 do 的前提（goto/assert 常见）：记下来但不产生执行项
        #
        # ★ type=assert 的前提**不能直接丢**。
        # 实测（168）：YAML 有 4 条前提，只有 1 条（clean，有 do）落地，
        # 2 条 `type: assert`（含"★起点：当前必须停在日历首页"）被静默跳过。
        # YAML 自己写了为什么必须校验：
        #   "不校验起点会导致 s1 的 iv_more 点击落空并在无报错的情况下失败"
        # 所以：有 expect 的 assert 前提转成 Precondition.assert_start，
        # 由 start_check 节点在开工前真的验一遍。
        if ptype == "assert":
            # ⚠️ 键名是 `check`，**不是** `expect`（实测踩过：按 expect 取值全是
            #    None，于是静默跳过。YAML 自己没写错，是我猜错了字段名）。
            #    第 4 条就是这样丢的：
            #      - type: assert
            #        check: {activity: .AllInOneActivity}
            start_assert, ewarns = _expect_to_landing(
                entry.get("check") if entry.get("check") is not None
                else entry.get("expect"))
            if start_assert:
                return Precondition(kind="data", desc=desc, do=[],
                                    undo=[], skip_if=None,
                                    assert_start=start_assert), warns + ewarns
            return None, warns + ewarns + [
                f"前提 assert（{desc[:30]}）没有可用的 check/expect，无法校验 —— 已跳过"]

        # ★ type=goto 已经在上面（`if not cmds` 之前）处理过了 ——
        #   因为 goto 即使写了 `do`，也还是 goto，不该掉进这里被当 data。
        return None, warns + [f"前提无 do 且非 goto/assert，已跳过：{desc[:30]}"]

    kind = "env" if ptype == "env" else "data"
    skip_if = entry.get("skip_if")
    return Precondition(kind=kind, desc=desc, do=cmds,
                        undo=_as_list(entry.get("undo")),
                        skip_if=skip_if if isinstance(skip_if, dict) else None), warns


_COORD_RE = re.compile(r"\[\s*-?\d+\s*,\s*-?\d+\s*\]")

# 步骤里**我们真的会读**的字段。其余一律报警 —— 写了不生效是最坏的失败模式。
_KNOWN_STEP_KEYS = {
    "id", "desc", "target", "locate", "landing", "pitfalls",
    "do", "expect", "know", "on_fail", "verify", "pitfalls_from",
    # 自动生成时，一步试过多种走法就写成 targets_tried（列表），
    # 单个走法是 target。它是"上次这么走过"的参考，不含坐标。
    "targets_tried",
    "locate_seq",   # 已废弃，但单独给一条更准确的警告，见下面
}

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
_KNOWN_TOP_KEYS = {
    "meta", "steps", "assertions", "preconditions",
    "per_step_pitfalls",   # 已废弃，但出现时给专门提示
    "confidence",          # 用例可信度（level/reason/todo）
    "playbook",            # 元规则，会被读成标题
}

# meta 段
_KNOWN_META_KEYS = {
    "case_id", "title", "package",
    "knowledge_files",
    "derived_from",        # 从哪个原始用例派生的
    "source", "note",
}

# assertions 段的一条断言
_KNOWN_ASSERT_KEYS = {
    "id", "desc", "check",
    # result: 用例原文里的预期写法，纯文本、不参与判定。
    # 有了它，规则描述才和原文对得上。
    "result",
    "note",
}

# preconditions 段
_KNOWN_PRE_KEYS = {
    "kind", "desc", "do", "undo", "skip_if",
    "assert_start", "target", "hint", "expect",
}

# 步骤里的 know 段
_KNOWN_KNOW_KEYS = {
    "where", "why_this", "locate", "source", "files",
}


def check_unknown_keys(raw: Any) -> list[str]:
    """扫出**所有层**的未知字段。返回警告文本（空 = 全部合法）。

    只报告、不改数据：未知字段本来就该被忽略，报出来是为了让人知道
    "你写的这个东西没生效"，而不是让 loader 崩掉。
    """
    if not isinstance(raw, dict):
        return []

    out: list[str] = []

    def scan(d: Any, allowed: set[str], where: str) -> None:
        if not isinstance(d, dict):
            return
        unknown = [k for k in d if k not in allowed]
        if unknown:
            out.append(
                f"{where}: 不认识的字段 {unknown} —— **未参与执行**。"
                f"可用字段：{sorted(allowed)}")

    scan(raw, _KNOWN_TOP_KEYS, "顶层")
    scan(raw.get("meta"), _KNOWN_META_KEYS, "meta")

    for i, a in enumerate(_as_list(raw.get("assertions"))):
        if not isinstance(a, dict):
            continue
        scan(a, _KNOWN_ASSERT_KEYS, f"断言 {a.get('id') or i + 1}")

    for i, pr in enumerate(_as_list(raw.get("preconditions"))):
        if not isinstance(pr, dict):
            continue
        scan(pr, _KNOWN_PRE_KEYS, f"前提 {i + 1}")

    for s in _as_list(raw.get("steps")):
        if not isinstance(s, dict):
            continue
        sid = s.get("id") or "?"
        scan(s, _KNOWN_STEP_KEYS, f"步骤 {sid}")
        scan(s.get("know"), _KNOWN_KNOW_KEYS, f"步骤 {sid}.know")

    return out



def find_stale_patterns(node: Any, path: str = "") -> list[tuple[str, str]]:
    """扫出 YAML 里**过期机制**的痕迹：坐标 / locate_seq。

    为什么这两样都要扫（见 docs/执行机制.md 第 3 节）：
      · 坐标 —— 这一刻这一屏的产物，换设备/分辨率/布局就失效
      · locate_seq —— 按顺序重放的动作序列，是"剧本"不是"参考"

    坐标有两种写法，**都要抓到**：
      · 字符串 `"tap": "x=100,y=200"` 或 `"x=100,y=200"`（--expect 语法）
      · 列表   `"tap": [100, 200]`                     ← YAML 里更常见
    早先只判字符串，列表形式整个漏掉 —— 而 168 老用例正是列表写法。
    """
    hits: list[tuple[str, str]] = []

    if isinstance(node, dict):
        for k, v in node.items():
            kp = f"{path}.{k}" if path else str(k)
            if k == "locate_seq":
                hits.append((kp, "locate_seq（按序重放的旧机制）"))
                continue
            hits.extend(find_stale_patterns(v, kp))
        return hits

    if isinstance(node, list):
        # ★ 纯数字对 / 数字列表 = 坐标（[100, 200]）
        nums = [x for x in node if isinstance(x, (int, float))]
        if len(nums) >= 2 and len(nums) == len(node) and not isinstance(node[0], bool):
            hits.append((path, f"坐标 {node}"))
            return hits
        for i, v in enumerate(node):
            hits.extend(find_stale_patterns(v, f"{path}[{i}]"))
        return hits

    if isinstance(node, str) and _COORD_RE.search(node):
        hits.append((path, f"坐标 {node}"))
    return hits


def purge_stale_case_yaml(p: Path, raw: Any, *, keep: bool = False) -> list[str]:
    """带过期机制的用例 YAML：删掉，回退到"首次执行"。

    ★ 用户定的规则：「如果当前的YAML里面已经有旧坐标了那就直接删除了。
                     我们重新执行生成新的YAML文件」

    为什么不迁移：旧 YAML 里的坐标不只是"一个过期数字"，它代表整份文件的
    思路是"按位置重放"。即使把坐标剔掉，locate_seq 的顺序剧本、按坐标推断
    出来的落点判据都还带着旧假设。逐字段修补比重写更容易出错，
    而重跑一遍本来只要几分钟。

    ⚠️ 删之前把原文件归档到 `_deleted/` —— 项目没有 git，删了就是真没了。

    ⚠️ `keep=True` 时**只报不删**：给格式校验类场景用（比如故意写错的样本
       要留着反复验证加载器），不然它们会被自己删掉、校验就再也没东西可验。
    """
    hits = find_stale_patterns(raw)
    if not hits:
        return []

    import logging
    from datetime import datetime

    log = logging.getLogger("android_agent.cases")
    reasons = "；".join(f"{k}: {v}" for k, v in hits[:5])

    if keep:
        return [f"{p.name} 含旧机制（{reasons}）"]

    archive = Path(p).parent / "_deleted"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = archive / f"{p.stem}.{stamp}.yaml"
    try:
        backup.write_text(Path(p).read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        backup = None

    try:
        Path(p).unlink()
        log.warning("🗑️  已删除含旧机制的用例 YAML：%s（%s）", p.name, reasons)
        if backup:
            log.warning("   原文件归档在：%s", backup)
    except OSError as e:
        log.error("删除失败 %s：%s（请手动删除）", p.name, e)
        return [f"{p.name} 含旧机制（{reasons}）且删除失败：{e}"]

    return [f"{p.name} 含旧机制（{reasons}）已删除，将按首次执行重新生成"]


def load_case(path: str | Path, *, keep_stale: bool = True) -> tuple[Any, list[str]]:
    """读一个 YAML 用例。返回 (CaseSpec, warnings)。

    ⚠️ **默认不删文件**（keep_stale=True）。要删必须显式说。

    为什么默认反过来：`load_case` 是个读函数，读一下就删掉磁盘上的文件，
    这个副作用太狠了。实测就出过事 —— 测试里只是遍历目录读一遍做校验，
    结果把 _test_invalid.yaml（一份故意写错的样本）给删了，
    校验顿时没东西可验。真正要跑用例的入口（run_case_json / runner）
    会显式传 keep_stale=False，那时删才是对的。

    warnings 非空 = 有字段没被采纳。**调用方必须把它们显示出来** ——
    静默忽略正是用例 YAML 反复警告的失败模式。
    """
    from ..agent.nodes import AssertSpec, Precondition, StepSpec
    from ..agent.runner import CaseSpec

    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # ★ 含旧机制（坐标 / locate_seq）的用例 YAML —— 删掉，当首次执行。
    #   注意：**只针对真实要跑的用例**。格式校验类场景传 keep_stale=True，
    #   否则那份故意写错的样本会被自己删掉，校验就再也没东西可验了。
    stale = purge_stale_case_yaml(p, raw, keep=keep_stale)
    if stale and not keep_stale:
        raise StaleCaseYaml("用例 YAML 含旧机制，已删除，需重新执行生成："
                            + "；".join(stale))

    warns: list[str] = []
    # ★ 先扫未知字段（全层）。这是"不能凭空添加字段"的总闸。
    #   放在最前面：即使后面的解析分支有早退，这条路也一定被检查过。
    warns += check_unknown_keys(raw)

    # 保留模式下把"这文件有旧机制"记进 warning，让调用方看得见但不拦。
    # locate_seq 现在**不参与执行**（没有回放了），留着只是噪音。
    if keep_stale:
        warns += stale

    meta = raw.get("meta") or {}
    case_id = str(meta.get("case_id") or p.stem)
    title = str(meta.get("title") or p.stem)
    package = str(meta.get("package") or "")
    if not package:
        warns.append("meta.package 缺失 —— 无法区分系统权限框与 App 弹窗")

    # ── 前提 ──
    preconds: list[Precondition] = []
    for i, entry in enumerate(_as_list(raw.get("preconditions"))):
        if not isinstance(entry, dict):
            warns.append(f"前提 {i+1} 不是字典，已跳过")
            continue
        pc, w = _precond_from(entry, i)
        warns += w
        if pc is not None:
            preconds.append(pc)
        # skip_if 已实现（见 child_agent._skip_if_met）。但**判不了的**条件
        # 会保守照跑，这里提示一下，免得用户以为"写了就一定跳过"。
        sk = entry.get("skip_if")
        if isinstance(sk, dict):
            unknown = [k for k in sk
                       if k not in ("activity", "package", "pkg", "state",
                                    "data_state", "timetable_state",
                                    "no_dialog", "dialog_absent")]
            if unknown or any(k in sk for k in ("state", "data_state",
                                                "timetable_state")):
                warns.append(
                    f"前提 `{str(entry.get('desc'))[:24]}` 的 skip_if 含"
                    f"判不了的键（{unknown or list(sk)}）—— **会保守照跑**")

    # ── 步骤 ──
    steps: list[StepSpec] = []
    for i, s in enumerate(_as_list(raw.get("steps"))):
        if not isinstance(s, dict):
            warns.append(f"步骤 {i+1} 不是字典，已跳过")
            continue
        sid = str(s.get("id") or f"s{i+1}")

        # ★ 未知字段必须报出来 —— 绝不静默忽略。
        #
        # 用户的核心要求是"用代码约束，不靠模型自觉"。而 YAML 里写了一个
        # 我们不认识的键，等于**那条约束根本没生效**，却看起来写了 ——
        # 这正是最该被消灭的"看着验了其实没验"。
        # 实测踩过：按 `expect` 取 assert 前提的判据，全取成 None、静默跳过，
        # 而 YAML 自己写的是正确的 `check`。
        _unknown_step = [k for k in s if k not in _KNOWN_STEP_KEYS]
        if _unknown_step:
            warns.append(
                f"{sid}: 不认识的字段 {_unknown_step} —— 未参与执行，"
                f"确认是否需要实现")

        # ★ 两种写法都支持，**直接字段优先**：
        #
        #   ① 直接写工具字段（新规范，docs/执行机制.md）：
        #        target: rid=iv_more          ← 强证据目标（代码能直点）
        #        landing: activity=.Foo
        #
        #   ② 旧格式（靠本文件翻译）：
        #        do: [{tap: {rid: iv_more}}]
        #        expect: {activity: .Foo}
        #
        # ⚠️ 曾经只认 ②，于是按 ① 写的 YAML **能通过格式检查、却完全读不进来**
        #    （locate/landing 全是 None，等于没有判据）。这种"写了不生效"
        #    正是 SPEC 要消灭的东西，加载器不能自己再犯一次。
        locate, _old_seq, w1 = _tap_to_locate(_as_list(s.get("do")))
        landing, w2 = _expect_to_landing(s.get("expect"))
        warns += [f"{sid}: {m}" for m in w1 + w2]

        # ★ target 是现役写法。locate 作为**同义旧名**继续支持 ——
        #   老 YAML 里写 `locate: rid=x` 意思完全一样，没必要逼人改名。
        direct_target = s.get("target") or s.get("locate")
        target = str(direct_target).strip() if direct_target else None

        if s.get("locate_seq"):
            # locate_seq 已经不参与执行了（没有回放机制）。
            # 这里**不静默忽略** —— 它意味着这一步原本想连点两下，
            # 只取第一个会漏动作、整步走不完。必须说出来让人拆成多步。
            warns.append(
                f"{sid}: `locate_seq` 已废弃（不再按序重放），"
                f"本步只会用第一个目标执行 —— 请拆成多步，每步一个 target")

        direct_landing = s.get("landing")
        if direct_landing:
            landing = str(direct_landing).strip() or None

        # 坑：用例 YAML 的 pitfalls 是混合类型（字符串 / {text,from,quote_hash}）
        pitches: list[str] = []
        for pt in _as_list(s.get("pitfalls")):
            if isinstance(pt, str):
                pitches.append(pt)
            elif isinstance(pt, dict) and pt.get("text"):
                pitches.append(str(pt["text"]))
                if pt.get("from"):
                    # 引用锚是给 kb_check 用的，我们暂时只当注解带上
                    pitches[-1] += f"（出处：{pt['from']}）"

        # know 是"这一步的知识" —— 用例 YAML 内联在文件里。
        # 我们的知识来自 knowledge_files 检索，所以把它并进 pitfalls，
        # 否则这段信息就丢了（而它恰恰是"为什么这么点"的说明）。
        know = s.get("know")
        if isinstance(know, dict):
            for k in ("where", "why_this", "locate"):
                if know.get(k):
                    pitches.append(f"[know.{k}] {str(know[k]).strip()}")
        elif know:
            warns.append(f"{sid}: know 形状不认识（{type(know).__name__}）")

        if s.get("on_fail"):
            # on_fail 目前只记不分支（分支行为要实现才知道怎么做）
            pass

        steps.append(StepSpec(id=sid, desc=str(s.get("desc") or sid),
                              landing=landing, locate=target,
                              target=target, pitfalls=pitches))

    # ── 断言 ──
    asserts: list[AssertSpec] = []
    for i, a in enumerate(_as_list(raw.get("assertions"))):
        if not isinstance(a, dict):
            warns.append(f"断言 {i+1} 不是字典，已跳过")
            continue
        aids = str(a.get("id") or f"a{i+1}")
        asserts.append(AssertSpec(id=aids, desc=str(a.get("desc") or aids),
                                  check=a.get("check"),
                                  # ★ 这三个以前被静默丢弃（AssertSpec 没声明）。
                                  #   现在声明了就必须真的读进来，否则等于没声明。
                                  note=str(a.get("note") or ""),
                                  result=str(a.get("result") or ""),
                                  # ★ 断言绑定的步骤 id（如 "s3"）—— 判定时
                                  #   据此取"那一步"的界面证据。不读进来的话，
                                  #   所有断言都只能拿整轮最后一屏硬判。
                                  step=str(a.get("step") or "")))

    # ── 文件级注解（不参与判定，但要**如实带出去**）──
    #
    # 这两个字段都不进机器判定通路（原本只被渲染成同名 .md 里的小节）。
    # 所以**不该当"未采纳"报警** —— 它们本来就不是给执行器用的。
    #
    # 但也不能直接扔：
    #   · playbook 的「按步读坑」是一条**元规则**，解释为什么坑挂在步骤上。
    #     这条跟我们的执行方式一致（确实按步喂 pitfalls），值得接进去。
    #   · confidence 说明"这条用例可不可信"。level != verified 时，
    #     主 Agent 该知道"这次 PASS 不代表它稳定"。
    playbook_titles = [
        str(b.get("title") or "") for b in _as_list(raw.get("playbook"))
        if isinstance(b, dict)
    ]
    conf = raw.get("confidence") or {}
    if not isinstance(conf, dict):
        conf = {}

    spec = CaseSpec(case_id=case_id, title=title, package=package,
                    steps=steps, asserts=asserts, preconditions=preconds,
                    knowledge_files=_as_list(meta.get("knowledge_files")),
                    per_step_pitfalls=True,
                    case_confidence={
                        "level": str(conf.get("level") or ""),
                        "reason": str(conf.get("reason") or "").strip(),
                        "todo": [str(t) for t in _as_list(conf.get("todo"))],
                    })
    return spec, warns


def find_case(name: str) -> Path | None:
    """按文件名片段找用例 YAML（如 '168'）。"""
    d = cases_dir()
    if not d.is_dir():
        return None
    for f in sorted(d.glob("*.yaml")):
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
