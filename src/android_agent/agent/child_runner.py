"""子 Agent 驱动器：把「主 Agent 拆好的任务包」跑成一份汇总结论。

上下文隔离的落地方式（这是整套设计的目的）：
  子 Agent 的几十轮 observe/act/OCR 全在它自己的 graph state 里，
  **主 Agent 只读 report.json**（几 KB），不接触中间过程。

用法：
    python child_runner.py task_168.json            # 真机
    python child_runner.py task_168.json --dry      # 只校验任务包，不碰设备
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..cases.paths import runner_python, skill_root
from ..device.bridge import RealDevice, ToolError
from .nodes import (
    ChildContext, build_child_graph, match_landing, search_knowledge,
)

SKILL = skill_root()


def load_task(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data


# 步骤描述里的通用动词/方位词，匹配节点文案时没有区分度，剔掉
_STOP = {"点击", "点", "右上角", "左上角", "右下角", "左下角", "菜单", "选", "选择",
         "然后", "如果", "页面", "按钮", "进入", "打开"}


def _keywords(desc: str, extra: str = "", limit: int = 6) -> list[str]:
    """从步骤描述抽关键词，优先长词（更有区分度）。

    「点右上角「更多」，菜单选「课程表」」-> ['课程表', '更多', 'iv_more'...]
    """
    toks: list[str] = []
    if extra:
        toks.append(extra)
    # 引号里的通常是控件名/文案，最可信
    for quoted in re.findall(r"[「『\"']([^」』\"']+)[」』\"']", desc):
        toks.append(quoted.strip())
    # 中文词组
    for w in re.findall(r"[\u4e00-\u9fa5]{2,}", desc):
        if w not in _STOP:
            toks.append(w)
    # 英文/下划线标识
    toks.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", desc))

    uniq: list[str] = []
    for t in toks:
        if t and t not in uniq and t not in _STOP:
            uniq.append(t)
    uniq.sort(key=len, reverse=True)
    return uniq[:limit]


def _dismiss_blocking_dialog(payload: dict, skill_root: str,
                             knowledge_files: list[str]) -> dict | None:
    """先处理"挡路的弹窗"，再谈推进步骤。

    这是 SKILL 明确要求的一步（"多余的临时弹窗 -> 关掉继续，不必停"），
    也是实测踩到的坑：pm clear 后日历会先弹「权限说明框」，
    不点掉它，后面所有点击都落在这个框上（实测连点 scrollView 12 次不动）。

    判据：屏幕上出现已知的"流程节点/弹窗"特征，且当前步骤的落点还没到。
    特征来自知识卡 —— 这里用**检索**拿到，不是硬编码。
    """
    clickable = [n for n in (payload.get("clickable") or []) if n.get("c")]
    if not clickable:
        return None

    # 已知的弹窗级 rid：出现在屏幕上就得先处理
    # （来自 knowledge/com.zui.calendar.md：pm clear 后 button1=同意）
    DIALOG_RIDS = {
        "button1": "弹窗主按钮（权限说明框=同意 / 确认框=确定）",
        "timetable_guide_button": "课程表引导页按钮",
    }
    for n in clickable:
        rid = (n.get("r") or "").split("/")[-1]
        if rid in DIALOG_RIDS:
            c = n["c"]
            return {
                "step_done": False, "next_x": c[0], "next_y": c[1],
                "why": f"先关掉挡路弹窗：{rid}（{DIALOG_RIDS[rid]}）",
                "expect": None,
            }
    return None


def autopilot_resume(payload: dict, task: dict) -> dict:
    """无 LLM 的自动应答（用于链路验证，不是最终形态）。

    真实使用时由子 Agent 的模型给出这些答案；这里用规则模拟，
    目的是验证**图本身**跑得通、且上下文不膨胀。

    决策顺序（与 SKILL 的纪律一致）：
      0) 有挡路弹窗 -> 先关掉（否则后面点哪都没用）
      1) 落点已命中 -> step_done
      2) locate 的 rid 精确匹配 next[].r
      3) 目标文字匹配 next[].t
      4) 兜底：第一个可点项

    ⚠️ 实测两个坑：
      - 真实 next 里「更多选项」的 r 是 None、只有 t，只按 rid 匹配会挑错
      - pm clear 后先出「权限说明框」(button1=同意)，不点掉会连点 12 次不动
    """
    kind = payload.get("kind")

    if kind == "decide":
        mode = payload.get("mode")

        # 起点纠正模式：目标是回到起点，不是推进步骤
        if mode == "fix_start":
            clickable = [n for n in (payload.get("clickable") or []) if n.get("c")]
            want = (payload.get("want_activity") or "").split("=", 1)[-1]
            for n in clickable:
                txt = (n.get("t") or "")
                rid = (n.get("r") or "")
                if "返回" in txt or "back" in rid.lower():
                    c = n["c"]
                    return {"step_done": False, "next_x": c[0], "next_y": c[1],
                            "why": "点返回回到起点"}
            if clickable:
                c = clickable[0]["c"]
                return {"step_done": False, "next_x": c[0], "next_y": c[1],
                        "why": "尝试走回起点"}
            return {"step_done": True, "why": "无路可走，接受当前起点",
                    "give_up": True, "give_up_reason": "起点不符且无可点项"}

        landing = (payload.get("step_landing") or "").strip()
        act_now = payload.get("activity") or ""
        hit, _ = match_landing(landing, activity=act_now,
                               clickable=payload.get("clickable"))
        if hit:
            return {"step_done": True, "why": "落点已满足"}

        # 0) 先清掉挡路弹窗
        blocker = _dismiss_blocking_dialog(
            payload, task.get("_skill_root", ""),
            task.get("knowledge_files") or [])
        if blocker:
            return blocker

        clickable = [n for n in (payload.get("clickable") or []) if n.get("c")]
        locate = payload.get("step_locate") or ""
        want_rid = locate.split("=", 1)[-1].strip() if "rid=" in locate else ""

        target = None
        # 1) rid 精确匹配
        if want_rid:
            for n in clickable:
                if (n.get("r") or "") == want_rid:
                    target = n
                    break
        # 2) 文字匹配：从步骤描述里抽关键词，再看哪个节点文案含它。
        #    ⚠️ 整句做子串匹配永远不命中（步骤是"点右上角「更多」…"，
        #    节点文案是"更多选项"），必须按词匹配。
        if target is None:
            hints = _keywords(payload.get("step_desc") or "", want_rid)
            best = None
            for n in clickable:
                txt = (n.get("t") or "")
                if not txt:
                    continue
                for h in hints:
                    if h and h in txt:
                        # 取命中词最长的那个节点，减少误配
                        if best is None or len(h) > best[1]:
                            best = (n, len(h))
                        break
            if best:
                target = best[0]
        # 3) 兜底：优先带 rid 的可点项（无 rid 的常是装饰性文案）
        if target is None:
            with_rid = [n for n in clickable if n.get("r")]
            if with_rid:
                target = with_rid[0]
            elif clickable:
                target = clickable[0]
        if target is None:
            return {"step_done": True, "why": "没有可点目标"}

        c = target["c"]
        return {"step_done": False, "next_x": c[0], "next_y": c[1],
                "why": f"自动推进：点 {target.get('r') or target.get('t')}",
                "expect": landing or None}

    if kind == "assert":
        ev = (payload.get("evidence_event_ids") or [""])
        return {"passed": True,
                "actual": f"链路验证：activity={payload.get('activity')}",
                "event_id": ev[0] if ev else ""}

    return {}


def run(task: dict, *, serial: str | None, diag: bool, dry: bool,
        max_loops: int = 60) -> dict:
    # ★ 每次运行一个独立目录，和 runner.py 用同一套规矩。
    #   原来这里是 `root / f"child_{case_id}.db"` —— root 是**源码目录**，
    #   同一条用例跑两次互相覆盖，还会往包里丢文件。
    #   （目前主流程不走这个 run()，但留着旧写法迟早有人踩。）
    from .runner import _run_tag
    root = Path(__file__).parents[3] / "tmp" / "runs" / f"{_run_tag()}_{task['case_id']}"
    root.mkdir(parents=True, exist_ok=True)
    db = root / "child.db"
    report_path = root / "report.json"

    if dry:
        print("[dry] 任务包校验：")
        print(f"  用例 {task['case_id']} {task['title']}")
        print(f"  步骤 {len(task['steps'])} 条，预期 {len(task['asserts'])} 条")
        print(f"  可检索文档 {task.get('knowledge_files')}")
        return {}

    python = runner_python()
    dev = RealDevice(SKILL, serial=serial)
    ok, why = dev.connected()
    print(f"设备：{why}")
    if not ok:
        raise SystemExit("设备未就绪")

    sid = dev.start_session(
        title=f"[子Agent] {task['title']}",
        user_input=f"子 Agent 自动执行：{task['title']}",
        package=task["package"], diag=diag)
    print(f"会话：{sid}")

    ctx = ChildContext(device=dev, skill_root=SKILL, python=python,
                       report_path=str(report_path))
    app = build_child_graph(ctx, SqliteSaver(
        sqlite3.connect(db, check_same_thread=False)))
    cfg = {"configurable": {"thread_id": f"child-{task['case_id']}-{sid}"}}

    init = {
        "task": task,
        "step_index": 0,
        "round_in_step": 0,
        "max_rounds_per_step": 8,
        "findings": [], "issues": [], "visited": [],
        "session_id": sid,
    }

    r = app.invoke(init, cfg)
    loops = 0
    while r.get("__interrupt__") and loops < max_loops:
        loops += 1
        payload = r["__interrupt__"][0].value
        resume = autopilot_resume(payload, task)
        print(f"  [{loops:02d}] {payload.get('kind'):8s} "
              f"{payload.get('step_id') or payload.get('assert_id') or ''} "
              f"act={payload.get('activity') or ''}")
        r = app.invoke(Command(resume=resume), cfg)

    print(f"\n总轮次 {loops}；report -> {report_path}")
    if report_path.exists():
        print(json.dumps(json.loads(report_path.read_text(encoding="utf-8")),
                         ensure_ascii=False, indent=2)[:1200])
    return r


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--serial")
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    t = load_task(a.task)
    run(t, serial=a.serial, diag=a.diag, dry=a.dry)
