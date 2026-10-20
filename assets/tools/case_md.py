"""把 storage/cases/*.yaml 渲染成人读的 Markdown（供前端/人审阅）。

设计立场：**YAML 是唯一执行真相（source of truth），MD 是它的渲染产物。**
所以 MD 由本脚本自动生成、可随时重生成，**不要手改 MD**（改了下次渲染会被覆盖）。
人若要改行为，改 YAML；本脚本只负责"让 YAML 好读"。

用法：
    python tools/case_md.py                 # 渲染全部
    python tools/case_md.py --case 68       # 只渲染某用例
    python tools/case_md.py --check         # 只校验 YAML 可解析，不写文件
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台默认 GBK，输出 ✅/⚠️ 这类字符会 UnicodeEncodeError。
# 统一包一层 UTF-8，避免因为一个对勾把整个脚本搞崩（实测踩过）。
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

CASES_DIR = Path(__file__).resolve().parent.parent / "storage" / "cases"

_LEVEL_BADGE = {
    "verified": "✅ 多会话验证",
    "single-run": "⚠️ 仅跑通一次",
    "flaky": "🔁 时好时坏",
    "device-specific": "📱 仅特定设备",
    "unreproducible": "🚫 本机不可复现",
}


def _fmt(v) -> str:
    """把任意标量/列表渲染成一行紧凑文本。"""
    if isinstance(v, list):
        return " / ".join(_fmt(i) for i in v)
    if isinstance(v, dict):
        return "，".join(f"{k}={_fmt(x)}" for k, x in v.items())
    return str(v)


def _fmt_pitfall(p) -> str:
    """pitfall 现在**只允许纯字符串**（StepSpec.pitfalls 是 list[str]）。

    旧的 `{text, from, quote_hash}` 写法已废弃 —— 那种 dict 模型读不到，
    `case_spec_check.py` 会判 DEAD。这里保留 dict 分支只是为了
    渲染历史文件时不炸，渲染结果与字符串一致（丢掉引用锚）。
    """
    if isinstance(p, dict):
        txt = str(p.get("text") or "").strip()
        return txt.lstrip("⚠️ ").strip()
    return str(p).lstrip("⚠️ ").strip()


def _collect_anchors(doc, out=None) -> list[tuple[str, int]]:
    """汇总文档里所有引用锚，按出现次数降序返回 [(ref, count)]。"""
    if out is None:
        out = []
    if isinstance(doc, dict):
        f = doc.get("from")
        if isinstance(f, str) and "#" in f:
            out.append(f)
        for v in doc.values():
            _collect_anchors(v, out)
    elif isinstance(doc, list):
        for v in doc:
            _collect_anchors(v, out)
    from collections import Counter
    return Counter(out).most_common()


def render(doc: dict) -> str:
    meta = doc.get("meta", {})
    out: list[str] = []
    add = out.append

    add(f"# {meta.get('title', '(无标题)')}")
    add("")
    add(f"> 自动生成自 `storage/cases/{meta.get('title', '')}.yaml` —— **勿手改本文件**，"
        f"改 YAML 后重跑 `python tools/case_md.py`。")
    add("")
    add(f"- **用例 ID**：{meta.get('case_id')}")
    add(f"- **包名**：`{meta.get('package')}`")
    src = meta.get("derived_from") or {}
    if src:
        add(f"- **来源**：会话 #{src.get('session')}（{src.get('status')}）"
            f"，{src.get('device')}，{_fmt(src.get('resolution'))}，{src.get('date')}")
    conf = doc.get("confidence") or {}
    if conf:
        lvl = conf.get("level", "")
        add(f"- **可信度**：{_LEVEL_BADGE.get(lvl, lvl)} —— {conf.get('reason', '')}")
    add("")

    # 执行须知：跟着这条用例走的操作约束（回放前必读）。
    # 为什么不放 SKILL.md：那种通用规约会被当背景噪音跳过；
    # 挂在用例顶部，回放这条用例时才会真的被读到。
    play = doc.get("playbook") or []
    if play:
        add("## ⚠️ 执行须知（回放前必读）")
        add("")
        for i, b in enumerate(play, 1):
            if isinstance(b, dict):
                add(f"### {i}. {b.get('title', '')}")
                add("")
                body = (b.get("body") or "").rstrip()
                if body:
                    add(body)
                    add("")
            else:
                add(f"- {b}")
        add("")

    pre = doc.get("preconditions") or []
    if pre:
        add("## 前置条件")
        add("")
        add("> 会话开始时**已冷启动复位**，所以起点一定是 App 首页。")
        add("> 按 `goto` → `prepare` → `assert` 的顺序处理；不满足即 **BLOCKED**。")
        add("")
        for p in pre:
            if not isinstance(p, dict):
                continue
            t = (p.get("type") or "assert").strip()
            badge = {"goto": "🧭 导航", "clean": "🧹 清数据",
                     "prepare": "🔧 准备", "assert": "🔒 断言"}.get(t, t)
            add(f"### {badge} · {p.get('desc')}")
            add("")
            if t == "goto":
                if p.get("target"):
                    add(f"- **目标界面**：{p['target']}")
                if p.get("path"):
                    add(f"- **怎么走**：{p['path']}")
            else:
                if p.get("do"):
                    add(f"- **怎么做**：{p['do']}")
                if p.get("check"):
                    add(f"- **判据**：`{_fmt(p.get('check'))}`")
            if p.get("skip_if"):
                add(f"- **已有则跳过**：`{_fmt(p.get('skip_if'))}`")
            if p.get("on_fail"):
                add(f"- **不满足**：**{p['on_fail']}**")
            if p.get("verified_by"):
                add(f"- **依据**：{p['verified_by']}")
            if p.get("note"):
                add("")
                add(str(p["note"]).rstrip())
            add("")

    add("## 步骤")
    add("")
    for s in doc.get("steps") or []:
        add(f"### {s.get('id')} · {s.get('desc')}")
        add("")
        if s.get("case_step"):
            add(f"- **对应用例步骤**：{s['case_step']}")
        add(f"- **动作**：`{_fmt(s.get('do'))}`")
        add(f"- **失败时**：**{s.get('on_fail')}**")
        know = s.get("know") or {}
        if know:
            add("- **执行须知**：")
            for k, v in know.items():
                add(f"  - `{k}`：{_fmt(v)}")
        exp = s.get("expect")
        if exp:
            add(f"- **落点断言**：`{_fmt(exp)}`")
        for pf in s.get("pitfalls") or []:
            add(f"- ⚠️ {_fmt_pitfall(pf)}")
        if s.get("evidence"):
            add(f"- 📎 证据：{s['evidence']}")
        add("")

    asserts = doc.get("assertions") or []
    if asserts:
        add("## 断言（对应用例预期）")
        add("")
        for a in asserts:
            add(f"### {a.get('id')} · {a.get('case_expect')}")
            add("")
            if a.get("desc"):
                add(f"- {a['desc']}")
            add(f"- **判据（机器可判）**：`{_fmt(a.get('check'))}`")
            if a.get("status_last_run"):
                add(f"- **上次结果**：{a['status_last_run']}")
            if a.get("evidence"):
                add(f"- 📎 证据：{a['evidence']}")
            add("")

    pf_all = doc.get("pitfalls") or []
    if pf_all:
        add("## 本用例的坑")
        add("")
        for p in pf_all:
            add(f"- ⚠️ {_fmt_pitfall(p)}")
        add("")

    # 引用锚汇总：让读 MD 的人一眼看到"哪些结论转抄自知识卡"（会过期）
    anchors = _collect_anchors(doc)
    if anchors:
        add("## 引用锚（这些结论转抄自知识卡，可能过期）")
        add("")
        add("> YAML 有意保留知识卡结论的精简副本（回放时不必翻卡），代价是可能过期。")
        add("> 用 `python tools/kb_check.py` 校验；报『引用已过期』时请复核对应小节。")
        add("")
        for ref, cnt in anchors:
            fname, _, sec = ref.partition("#")
            add(f"- `{fname}` → **{sec}**（被引用 {cnt} 处）")
        add("")

    todo = (doc.get("confidence") or {}).get("todo") or []
    if todo:
        add("## 待确认")
        add("")
        for t in todo:
            add(f"- [ ] {t}")
        add("")

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, help="只处理某个 case_id")
    ap.add_argument("--check", action="store_true", help="只校验，不写 MD")
    args = ap.parse_args()

    try:
        import yaml
    except ImportError:
        print("需要 PyYAML：pip install pyyaml", file=sys.stderr)
        return 2

    if not CASES_DIR.is_dir():
        print(f"目录不存在：{CASES_DIR}", file=sys.stderr)
        return 2

    files = sorted(CASES_DIR.glob("*.yaml"))
    if not files:
        print("没有 yaml 用例文件", file=sys.stderr)
        return 1

    rc = 0
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"❌ {f.name} 解析失败：{e}")
            rc = 1
            continue
        cid = (doc.get("meta") or {}).get("case_id")
        if args.case is not None and cid != args.case:
            continue
        if args.check:
            print(f"✅ {f.name} 可解析（case {cid}）")
            continue
        md = render(doc)
        out = f.with_suffix(".md")
        out.write_text(md, encoding="utf-8")
        print(f"✅ {f.name} -> {out.name}（{len(md)} 字符）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
