"""通用子 Agent 模块 —— 任何 case 都能跑的执行骨架。

设计目标（按需求）：
    确定点击某个 → 判断是否到达目标页面 → 检查是否满足 case 要求

使用方式（3 行）：
    from subagent import run_case, CaseSpec, StepSpec, AssertSpec

    report = run_case(
        CaseSpec(case_id="168", title="...", package="com.zui.calendar",
                 steps=[...], asserts=[...]),
        answer=my_llm_answer,      # 或省略 -> 自动模式
        serial="HA2DMRBK", diag=True)
    print(report.status, report.summary)

层的划分：
    ┌──────────────────────────────────────────┐
    │ 主 Agent：拆解用例、读 report.json        │  <- 上下文隔离在report处
    ├──────────────────────────────────────────┤
    │ 子 Agent（本模块）：                      │
    │   graph.py      流程骨架（本文件编排）     │
    │   CHILD_PROMPT  判断纪律（来自 SKILL.md）  │
    │   vision       看图能力（不依赖主模型）    │
    ├──────────────────────────────────────────┤
    │ skill 工具层：run.py 的 act/observe/...   │  <- 只经 run.py
    └──────────────────────────────────────────┘

case 之间只有**数据**不同（步骤/预期/知识卡），执行骨架完全共用。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..cases.paths import runner_python, skill_root
from ..device.bridge import (RealDevice, ToolError,
                             is_system_permission_pkg)
from ..device.direct import DirectDevice
from . import decide as dc
from .decide import DecisionError
from .nodes import (
    AssertSpec, AssertVerdict, ChildContext, StepAction, StepSpec,
    SubagentReport, SubagentTask, build_child_graph, match_landing,
    search_knowledge,
    _MODEL_NODES,   # 算模型耗时的节点全集，只在 nodes 里定义一份
)

DEFAULT_SKILL = skill_root()


# --------------------------------------------------------------------------
# 节点执行统计（次数 + 耗时）
# --------------------------------------------------------------------------
#
# 为什么不用 app.invoke() 而要 stream：invoke 只在结束时返回最终 state，
# 中间跑了哪些节点、每个节点花多久，全看不见。
#
# 167 的教训：日志里的 `'vision': 0` 让我误判"视觉没跑过"，
# 实际它跑了 6 次、每次都失败。差别在于那个 counts 只统计
# **会 interrupt 的节点** —— vision 不 interrupt，所以永远是 0。
#
# 换成 stream 之后，"哪些节点真的执行了"和"各花多久"都可以直接从
# 事件流里数出来，不再依赖人猜。
#
# 用户还提了「模型得时间也要统计在内」：模型调用（decide/assert）动辄
# 5~20 秒，是整轮耗时的大头。如果只报总耗时，看不出"慢在模型还是慢在设备"，
# 所以这里按节点分开记。

# 这些节点的耗时主要是**等模型**，单独归一类。
# ⚠️ 定义在 nodes 里（报告要用它算 model_s），这里**引用**而不是重新定义 ——
#    两处各写一份，早晚会出现"日志说 8s、报告说 6s"这种对不上的情况。


def _run_tag() -> str:
    """本次运行的证据子目录名：run_<年月日_时分秒>。

    为什么不用 run_id：落盘发生在落库之前，那时还没有 run_id。
    时间戳够用了 —— 同一个用例不可能在同一秒跑两次（有锁，且跑一次几十秒）。
    """
    import datetime as _dt
    return "run_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _new_node_calls() -> dict[str, dict[str, float]]:
    """节点统计表：{节点名: {"n": 次数, "ms": 累计毫秒}}"""
    return {}


def _bump(calls: dict[str, dict[str, float]],
          name: str, ms: float) -> None:
    rec = calls.setdefault(name, {"n": 0, "ms": 0.0})
    rec["n"] = rec["n"] + 1
    rec["ms"] = rec["ms"] + ms


def _fmt_node_calls(calls: dict[str, dict[str, float]]) -> str:
    """把统计表排成一行，次数多的在前。"""
    items = sorted(calls.items(), key=lambda kv: -kv[1]["n"])
    parts = [f"{k}×{int(v['n'])}/{v['ms'] / 1000:.1f}s"
             for k, v in items if v["n"]]
    return "  ".join(parts) if parts else "(无)"


def _drive(app, inp, cfg, calls: dict[str, dict[str, float]],
           log=None):
    """跑一段图，把节点次数/耗时顺手统计下来，返回最终 state。

    stream(stream_mode="updates") 每产出一个 {节点名: 增量} 就说明那个节点
    刚执行完 —— 数它、记它花的时间，然后把最终结果从 get_state 取出来。

    ⚠️ 为什么最终 state 用 app.get_state() 而不是自己合并增量：
       增量里有 Annotated[..., operator.add] 的字段（findings / issues /
       progress 等），手工合并等于把 LangGraph 的 reducer 重写一遍，
       漏一个就会出现"结果少一块"的诡异现象。get_state 拿的是
       checkpoint 里的权威 state，不重复造轮子。
    """
    import time as _t
    prev = _t.perf_counter()
    for chunk in app.stream(inp, cfg, stream_mode="updates"):
        now = _t.perf_counter()
        ms = (now - prev) * 1000
        # 一次 chunk 可能带多个节点（并发分支），逐个记
        for name in (chunk or {}):
            if name.startswith("__"):
                continue
            _bump(calls, name, ms)
        prev = now

    snap = app.get_state(cfg)
    values = dict(snap.values or {})

    # ★★★ 必须把 __interrupt__ 补回去，否则主循环立刻退出 ★★★
    #
    # 这是踩过的坑：app.invoke() 返回的 state 里**带** __interrupt__，
    # 主循环 `while state.get("__interrupt__")` 靠它判断"还没跑完"。
    # 换成 stream 之后我天真地以为 get_state().values 也是一样的 ——
    # 结果 values 里根本没有这个键（实测 stream_mode 的 updates/values
    # 两种都拿不到），于是第一段跑完循环就以为结束了，
    # 用例直接 BLOCKED、步数 0。7 个测试同时炸才把它揪出来。
    #
    # 中断信息实际在 snap.interrupts 里（StateSnapshot 的属性）。
    if getattr(snap, "interrupts", None) or getattr(snap, "next", None):
        values["__interrupt__"] = list(getattr(snap, "interrupts", ()) or ())

    # 收尾：把统计塞进 state，report 才能带着它一起落盘。
    try:
        app.update_state(cfg, {"node_calls": {
            k: {"n": int(v["n"]), "ms": round(v["ms"], 1)}
            for k, v in calls.items()}})
    except Exception:  # noqa: BLE001
        # 统计失败绝不能影响跑用例本身
        pass
    # update_state 之后再取一次（node_calls 才在里面），同样补中断
    snap2 = app.get_state(cfg)
    out = dict(snap2.values or {})
    if values.get("__interrupt__"):
        out["__interrupt__"] = values["__interrupt__"]
    return out


def _model_ms(calls: dict[str, dict[str, float]]) -> float:
    """模型相关节点的累计耗时（秒）。"""
    return sum(calls.get(n, {}).get("ms", 0.0)
               for n in _MODEL_NODES) / 1000.0


def _make_logger(log_path: Path | None):
    """返回一个 log(msg) —— 同时打到 stdout 和**落盘**。

    落盘是为了后台跑的时候能随时 tail 看进度；
    之前没有日志，出问题只能靠猜（实测卡在点击循环里看不出来）。

    ⚠️ 用追加模式，**不是覆盖**。
    踩过：`open("w")` 下同一条用例跑第二次，第一次的日志就被清掉了 ——
    而"第一次跑挂了、第二次跑好了"恰恰是最需要对比两次日志的时候。
    现在每条用例的日志都留着，靠开头的分隔行区分是哪一次。
    """
    fh = None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = log_path.open("a", encoding="utf-8", buffering=1)
        # 每次运行都打一条明显的分隔，否则连续跑两次分不清哪段是哪次
        fh.write(f"\n{'=' * 70}\n"
                 f"运行开始 {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                 f"{'=' * 70}\n")

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        if fh:
            fh.write(line + "\n")

    log.close = lambda: fh and fh.close()  # type: ignore[attr-defined]
    return log

__all__ = [
    "CaseSpec", "StepSpec", "AssertSpec", "SubagentReport", "SubagentTask",
    "run_case", "run_case_sync", "match_landing", "search_knowledge",
    "StepAction", "AssertVerdict", "DEFAULT_SKILL",
]


class CaseSpec(SubagentTask):
    """一个 case 的完整定义（= 主 Agent 交给子 Agent 的任务包）。

    继承 SubagentTask 以复用字段校验，同时给出更友好的构造提示。
    """

    @classmethod
    def from_json(cls, path: str | Path) -> "CaseSpec":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8")


# --------------------------------------------------------------------------
# 执行入口
# --------------------------------------------------------------------------

AnswerFn = Callable[[dict[str, Any]], dict[str, Any]]


def make_autopilot() -> AnswerFn:
    """无 LLM 的规则应答器（链路自测用；真实场景传入模型应答）。

    规则（对应 CHILD_PROMPT 的纪律）：
      decide -> 落点命中则 step_done；否则按 rid → 文字 → 兜底 挑目标
      assert -> 用最近证据绑定

    ⚠️ 这不是"AI 在判断"，是 if/else。留着只为链路自测与离线跑。
    真跑用例请用 make_model_answer()。
    """
    from .child_runner import autopilot_resume

    def _answer(payload: dict[str, Any]) -> dict[str, Any]:
        return autopilot_resume(payload, {})

    return _answer


def make_model_answer(*, skill_root: str = DEFAULT_SKILL,
                      python: str | None = None,
                      verbose: bool = True,
                      log: Callable[[str], None] | None = None
                      ) -> AnswerFn:
    """真模型应答器 —— decide/assert 两个判断点交给 LLM。

    这是把"AI 座位"从规则函数换成人（模型）的地方。三个判断点：
      1. decide 正常模式  -> 这一步做完了吗、下一步点哪
      2. decide 起点纠正  -> 不在起点时自己想办法走回去（不 Block）
      3. assert          -> 期望结果满足了吗

    知识卡检索由**图内**的 knowledge_node 负责（模型填 need_knowledge →
    路由到 knowledge 节点检索 → 命中片段放进 action["knowledge"] → 回到这里）。
    所以本函数只把上一轮查到的片段喂给模型，不自己检索，
    否则同一份文档会被取两次、且检索动作在图外不可见。

    坐标纪律由 decision_channel._validate_action 兜底：
    模型给的坐标必须来自当前响应的可点项，否则报错而不是硬点。
    """
    from . import decide as dc

    root = str(skill_root)
    # 解释器不在资产目录下（资产在 assets/，.venv 在仓库根），交给统一解析
    py = python or runner_python()
    emit = log or (lambda m: print(f"[model] {m}") if verbose else None)

    def _answer(payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("kind")
        # 图内 knowledge_node 查到的片段（没有就是空）
        know = str(payload.get("knowledge") or "")
        if know and emit:
            emit(f"本轮带上知识卡命中 {know.count('[')} 处")

        if kind == "decide":
            mode = payload.get("mode") or "step"
            fn = dc.decide_fix_start if mode == "fix_start" else dc.decide_step
            action = fn(py, root, payload, knowledge_text=know)
            if emit:
                if action.get("need_knowledge"):
                    emit(f"模型请求知识卡：{action['need_knowledge']}")
                elif action.get("give_up"):
                    emit(f"模型放弃：{action['give_up_reason']}")
                elif action.get("step_done"):
                    emit("模型判定：本步完成")
                else:
                    emit(f"模型决定点 ({action.get('next_x')},"
                         f"{action.get('next_y')})：{action.get('why')}")
            return action

        if kind == "assert":
            # ★ 断言阶段的模型回复格式错误，**不许**让整轮用例变 BLOCKED。
            #
            # 实测踩到（用户反馈）：模型其实已经看清了界面（PhotoPicker 起来了、
            # 图片网格都在），只是 reason 里带了裸双引号
            #   "看到 "拍摄于 2026年8月7日 上午11:46:18的照片""
            # _extract_json 抛 DecisionError → 一路冒到进程顶层 → exit=1
            # → 报告都没写出来。**一条回复的格式问题，杀掉了整轮执行。**
            #
            # 现在的处理：
            #   · 先给 _extract_json 加了一层"裸引号修复"（见 decision_channel）
            #   · 万一还是解析不了，**只把这一条断言判为 unknown**，
            #     不抛异常、不中断用例 —— 其余断言照跑，报告照写。
            #   · unknown 不等于 PASS 也不等于 FAIL，报告里如实体现，
            #     由主 Agent 决定是否重跑。**绝不猜成 PASS**（那才是真危险）。
            try:
                verdict = dc.assert_step(py, root, payload, knowledge_text=know)
            except DecisionError as exc:
                if emit:
                    emit(f"⚠️ 断言模型回复无法解析，本条记为 unknown（不中断用例）：{exc}")
                return {
                    "passed": None,          # None = 判不了，不是 False
                    "actual": f"模型回复无法解析：{str(exc)[:200]}",
                    "evidence": "（格式错误，未做判定）",
                    "event_id": "",
                    "confidence": "low",
                    "unknown": True,
                    "error": str(exc)[:400],
                }
            if emit:
                emit(f"断言 {'通过' if verdict.get('passed') else '不通过'}"
                     f"（{verdict.get('confidence')}）：{verdict.get('evidence')}")
            return verdict

        raise DecisionError(f"未知的中断类型：{kind}")

    return _answer


def run_case(spec: CaseSpec | dict[str, Any], *,
             answer: AnswerFn | None = None,
             serial: str | None = None,
             skill_root: str = DEFAULT_SKILL,
             diag: bool = True,
             max_rounds_per_step: int = 8,
             max_locate_attempts: int = 3,
             engine: str = "",
             max_loops: int = 80,
             workdir: str | Path | None = None,
             device: Any = None,
             case_yaml_path: str | Path | None = None,
             writeback: bool = True,
             yaml_warnings: list[str] | None = None,
             verbose: bool = True) -> SubagentReport:
    """跑一个 case，返回结构化报告。

    Args:
        spec:  用例定义（步骤 + 预期 + 知识卡）
        answer: 每轮中断的应答函数；None 则用规则应答器（仅链路自测）
        serial: 设备序列号；None 表示唯一设备
        diag:  True 标记为验证会话（不进测试记录、不计成功率）
        workdir: 存放 checkpoint db 与 report.json 的目录
        device: 注入的设备实现（默认 RealDevice）。传了就不查 adb，
                供单测验证图逻辑与记账，无需插真机。
        case_yaml_path: 这条用例的 YAML 文件。给了才**回写**实际路径
                （用户要求：「用我们的工具完成后要生成或者把旧的 YAML
                不合理的修改」）。不给就只报告、不动文件。
        writeback: 是否回写。用例 PASS/FAIL 都会写 —— 因为"实际怎么走到的"
                对两种结论都有价值；但 BLOCKED 不写（路径本身没走完，
                写进去会把半截路径固化成"经验"）。
        yaml_warnings: 加载用例 YAML 时**写了但没被采纳**的判据字段。
                必须进报告 —— 只 print 到 stdout 等于没人知道。
                扫描存量 8 个用例发现 14 种字段被丢弃（text_present /
                dialog_closed / package ...），这些判据从来没生效过。
        verbose: 是否打日志

    Returns:
        SubagentReport —— 主 Agent 只需要读这个（上下文隔离点）
    """
    if isinstance(spec, dict):
        spec = CaseSpec(**spec)
    task = spec.model_dump()
    # ★ 引擎必须进 task —— 报告文件由 finish 节点写、run_case 再读回来，
    #   事后给 report 对象赋值改不到文件里（实测踩过）。
    task["engine"] = engine

    # ★★ 把前提里的起点断言搬到 task["start_assert"]。
    #
    # 断链踩过（用户报告）：YAML 写了
    #     - desc: ★起点：当前必须停在**日历首页**（有 iv_more 工具栏的那一屏）
    #       type: assert
    #       expect: {activity: .AllInOneActivity}
    # 加载器把它存进 Precondition.assert_start，
    # 但 start_check_node 读的是 **task["start_assert"]** —— 两边没接上，
    # start_check 永远拿到 None，于是"起点不符"从来没被检出过。
    #
    # 后果正是 YAML 自己警告过的那条：
    #   "不校验起点会导致 s1 的 iv_more 点击落空并在无报错的情况下失败"
    #
    # 多条 assert 前提时取**第一条**（与 YAML 顺序一致，越靠前越基础）。
    if not task.get("start_assert"):
        for _p in (task.get("preconditions") or []):
            _sa = str(_p.get("assert_start") or "").strip()
            if _sa:
                task["start_assert"] = _sa
                break

    # ★ 运行产物统一落到 tmp/ —— 不写进源码目录。
    #
    # 早先这里是 `_HERE`（本文件所在目录）。文件挪进 agent/ 包以后，
    # `_HERE` 变成 src/android_agent/agent/，于是 .db / report / log
    # 全写进源码树里，越跑越脏，而且没有一个地方能统一清理。
    # 现在默认 tmp/，由 reports.scratch 按 24 小时自动清理。
    if workdir:
        root = Path(workdir)
    else:
        from ..report.scratch import tmp_dir
        root = tmp_dir()
    root.mkdir(parents=True, exist_ok=True)
    db = root / f"child_{spec.case_id}.db"
    report_path = root / f"report_{spec.case_id}.json"
    log_path = root / f"run_{spec.case_id}.log"
    log = _make_logger(log_path)
    if report_path.exists():
        report_path.unlink()

    t0 = time.time()

    def el() -> str:
        return f"[{time.time() - t0:6.1f}s]"

    log(f"=== 用例 {spec.case_id} {spec.title} ===")
    log(f"步骤 {len(spec.steps)} 条，预期 {len(spec.asserts)} 条")
    for p in spec.preconditions:
        log(f"前提[{p.kind}] {p.desc or ''} do={p.do} undo={p.undo}")

    # ⚠️ 每次运行必须用**全新**的 checkpoint 库。
    # 曾经复用 child_<case>.db + 固定 thread_id，重跑同一 case 会 resume 旧线程，
    # findings 累加（实测 steps_done 3 -> 15 -> 18 次次增长），结论完全失真。
    # checkpoint 是给"同一轮内中断恢复"用的，不是跨轮次累积用的。
    if db.exists():
        db.unlink()
    for sidecar in root.glob(f"{db.name}-*"):     # sqlite wal/shm
        sidecar.unlink(missing_ok=True)

    python = sys.executable
    # device 可注入 —— 否则单测必须插着真机才能跑。
    # 这不是为测试开后门：它让"图逻辑/记账"能在无设备环境下被验证，
    # 而真机行为仍由端到端用例覆盖。
    #
    # ★ 默认用 DirectDevice：in-process 直调库，不起 subprocess。
    #   老 RealDevice（每步 subprocess 调 run.py）仍保留在 device/bridge.py，
    #   需要会话台账/审计时可用 device=RealDevice(...) 显式注入。
    # ★ 点击账本：每次点击一条完整记录（控件 / 前后页面 / 耗时 / 画面变没变）。
    #   用户明确要求过"每一步点击了啥都要增加记录，好方便排查" ——
    #   只写坐标对人是没意义的，必须记清点到的是什么控件、点完变成了什么。
    ledger_path = root / f"clicks_{spec.case_id}.jsonl"
    if ledger_path.exists():
        ledger_path.unlink()      # 每次运行重新记，别把上次的混进来

    dev = device if device is not None else DirectDevice(
        serial=serial,
        # ★ 证据目录要**按运行隔离**，不能所有运行共用一个 shots/。
        #
        #   原来固定是 report_path.parent / "shots"，而 report_path 就叫
        #   report_<case_id>.json —— 于是同一个用例每跑一次都往
        #   tmp/shots/step03/、step04/ 里覆盖同一批文件名。
        #   后果：库里 10 条 167 记录全都记着 tmp/shots/step04/shot.png，
        #   而磁盘上那份是**最后一次**的，前面 9 条点开看到的是别的运行的图
        #   （还看不出来，因为路径和文件名都对）。删除记录时也就没法判断
        #   "这文件到底属于谁"，只能一律不删 —— 删除功能形同虚设。
        #
        #   现在每跑一次落一个 run_<时间戳>/ 子目录，各管各的。
        #   旧数据不动：它们本来就共享，删的时候按"还被别人引用"保留。
        evidence_dir=(Path(report_path).parent / "shots" / _run_tag()
                      if report_path else None),
        ledger_path=ledger_path)

    if device is None:
        ok, why = dev.connected()
        log(f"设备：{why}")
        if not ok:
            raise RuntimeError("设备未就绪")
    else:
        log("设备：（注入的假设备，跳过 adb 检查）")

    # 直连模式没有会话，这个 id 只用来拼 thread_id
    sid = dev.start_session(
        title=f"[子Agent] {spec.title}",
        user_input=f"子 Agent 自动执行：{spec.title}",
        package=spec.package)
    log(f"会话 {sid} 已开{el()}")

    # 节点统计表：第一次 _drive 之前就要有，
    # 否则 UnboundLocalError（第一次跑就是这个错）。
    node_calls = _new_node_calls()

    ctx = ChildContext(device=dev, skill_root=skill_root, python=python,
                       report_path=str(report_path))
    conn = sqlite3.connect(db, check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver

    app = build_child_graph(ctx, SqliteSaver(conn))
    cfg = {"configurable": {"thread_id": f"child-{spec.case_id}-{sid}"}}

    state = _drive(app, {
        "task": task,
        "step_index": 0,
        "round_in_step": 0,
        "max_rounds_per_step": max_rounds_per_step,
        # 「换目标找」的上限：找不到目标时允许换几次（用户要求有界）。
        "locate_attempts": 0,
        "max_locate_attempts": max_locate_attempts,
        # ★ 强证据直点：target 在当前屏唯一命中就代码直点，定不下来才叫模型。
        "fallback_reason": "",
        "fallback_guidance": "",
        "retry_landing": 0,
        "strong_hits": [],
        "obs_xml_path": "",
        "findings": [], "issues": [], "visited": [],
        # seen_rids / locate_tried / progress 用的是 operator.add 累加 reducer，
        # 必须给初值，否则第一轮就 `None + list` 报错。授权/弹窗留痕同理。
        "seen_rids": [],
        "seen_texts": [],
        "locate_tried": [],
        "progress": [],
        "permissions_granted": [],
        "dialogs_dismissed": [],
        "runtime_permissions": [],
        "runtime_permission_prompts": [],
        "session_id": sid,
    }, cfg, node_calls)

    from langgraph.types import Command

    answer_fn = answer or make_autopilot()
    loops = 0
    # 模型真正被调用的累计耗时。
    #
    # ⚠️ 不能拿 node_calls["decide"]["ms"] 当模型耗时 —— 实测 27 次 decide
    #    加起来才 353ms。因为 decide 节点本身**只负责发 interrupt**，
    #    真正等模型的是主循环里的 answer_fn（外面那一层）。
    #    所以模型耗时必须在这里掐表，不能在节点里算。
    model_ms = 0.0

    # 包一层，专门掐模型调用的表。写成闭包是为了只改这一处，
    # 而不是在三个调用点上各写一遍 time.perf_counter()（漏一个就少算）。
    _raw_answer = answer_fn

    def answer_fn(payload):  # noqa: F811
        nonlocal model_ms
        _t0 = time.perf_counter()
        try:
            return _raw_answer(payload)
        finally:
            model_ms += (time.perf_counter() - _t0) * 1000
    # ⚠️ 这个 counts 只能数**会 interrupt 的节点**（decide/assert），
    #    vision / knowledge / observe / act 都不 interrupt，永远是 0。
    #
    #    167 排查时我被它坑了一整轮：日志写着 `'vision': 0`，
    #    我据此判断"视觉节点压根没跑过"，于是去翻路由和图定义 ——
    #    可它明明跑了 6 次。**看着像数据的东西，其实是个空集。**
    #
    #    真正的节点执行次数在 node_calls 里（由 _drive 从 stream 里统计）。
    counts = {"decide": 0, "assert": 0, "vision": 0, "knowledge": 0}
    # 循环检测：连续对同一目标做同类决定，说明卡住了
    recent: list[str] = []
    # 每个坐标被"作废"过几次 —— 用于从告警升级为强制终止
    loop_blocks: dict[str, int] = {}
    # ★ 本屏已用掉几次"点弹窗不算连点"的豁免额度。
    #   没有上限的话，模型只要每轮都申报 is_dialog，连点计数就永远被清零，
    #   交替循环检测也永远看不到 3 个点 —— 真机 168 就这么空转到 80 轮。
    dialog_exempt: dict[str, int] = {}
    # 循环检测的作用域：当前是第几步。换步就清空上面的历史。
    last_loop_step = ""
    # 模型给出不合法坐标时的补偿次数（见下面 DecisionError 分支）。
    # 2 次足够：一次改正、一次确认；再多说明模型根本没看懂可点列表。
    retries = 0
    MAX_DECIDE_RETRIES = 2

    while state.get("__interrupt__") and loops < max_loops:
        loops += 1
        payload = state["__interrupt__"][0].value
        kind = payload.get("kind", "?")
        counts[kind] = counts.get(kind, 0) + 1

        if kind == "decide":
            mode = payload.get("mode", "step")
            tag = payload.get("step_id") or "-"
            # ★ 换步了就清空"点过哪些坐标"的历史。
            #
            # 实测踩到的坑（把 s1 的 rid 改错模拟跨版本）：
            # s1 撞死循环放弃后，s2/s3 **刚一进场就被判死循环** ——
            # 因为 recent 里还留着 s1 的 (337,203)/(1244,1276)，
            # 周期检测一算就是"交替点击无进展"。可 s1/s2/s3 的落点
            # 根本不同，根本不是同一个循环。
            #
            # 循环检测必须**按步**做：跨步的坐标没有可比性。
            # 之前没暴露是因为正常情况下 s1 不会积累到 6 个点。
            if tag != last_loop_step:
                recent.clear()
                loop_blocks.clear()
                dialog_exempt.clear()
                last_loop_step = tag
                # 补偿次数也按步重置 —— 换了一步就是新的上下文，
                # 不该让上一步用掉的额度连累这一步。
                retries = 0
            land = payload.get("step_landing") or "-"
            cl = payload.get("clickable") or []
            # 日志里只打前 4 个（屏上一二十个可点项时全打会把日志刷没），
            # 但**必须标明总数已被截断** —— 否则排查时看到"可点(8): 列了4个"，
            # 分不清是模型只拿到 4 个还是日志只显示了 4 个，会往错的方向查。
            tops = ", ".join(
                f"{(n.get('r') or n.get('t') or '?')}@{n.get('c')}" for n in cl[:4])
            if len(cl) > 4:
                tops += f" …（另有 {len(cl) - 4} 个，模型可见全部）"
            log(f"{el()} 轮{loops:02d} decide[{mode}] {tag} "
                f"act={payload.get('activity')} 落点={land}")
            log(f"           可点({len(cl)}): {tops or '(无)'}")
            # ★ 把"这一步已经点过哪些坐标"喂给模型。
            # 不给的话它看不见自己的重复，会一直点同一个地方
            # （实测：连点同一张照片 9 次，白跑 80s 才发现）。
            if recent:
                payload = {**payload, "history": [
                    f"已点过 {k}" for k in recent[-6:]
                ]}
                if len(recent) >= 3 and len(set(recent[-3:])) == 1:
                    payload = {**payload, "loop_warning":
                               f"你已连续 {len(recent)} 次点 {recent[-1]}，"
                               "这个动作显然无效，必须换一个目标"
                               "（换控件 / 滚动 / 返回 / 判定已完成）"}
        elif kind == "assert":
            log(f"{el()} 轮{loops:02d} assert {payload.get('assert_id')} "
                f"{payload.get('desc')}")
        else:
            log(f"{el()} 轮{loops:02d} {kind}")

        # ★ decide 阶段的格式错误：**终止本次决策**，不许让进程崩掉。
        #
        # 断言阶段可以降级成"判不了"，decide 阶段不行 —— 它要给出坐标，
        # 猜错坐标会点到不该点的地方（SKILL 里最严重的坑）。
        # 所以这里既不猜、也不让整个用例变 BLOCKED：
        # 把这一轮判成 give_up（"模型给不出可用动作"），让图正常收尾、
        # 报告正常写出来。用户看到的会是 BLOCKED/FAIL + 明确原因，
        # 而不是"执行器崩溃、报告没写入"这种没法归因的结果。
        try:
            ans = answer_fn(payload)
        except DecisionError as exc:
            log(f"           ⚠️ 模型回复无法解析：{str(exc)[:160]}")
            # ★★ 坐标不合法 ≠ 无路可走，**要给重试机会**。
            #
            # 实测（168，轮03）：模型把上一屏的坐标 (1795,1583) 又给了一遍，
            # 而那一屏已经不存在这个坐标了。早先这里直接 give_up，
            # 于是**一个可修正的笔误毁掉整步**，s1/s2/s3 三步连挂。
            #
            # 模型是能改的 —— 只要告诉它"这个坐标不在列表里，请重选"。
            # 所以这里按"可重试"处理：同一轮内最多补偿 RETRY 次，
            # 把错误原文回灌给下一次调用（answer_fn 会带上 retry_hint）。
            # 超过次数仍不改正才放弃 —— 那时确实是模型理解不了。
            if kind == "decide":
                retry_left = MAX_DECIDE_RETRIES - retries
                if retry_left > 0:
                    retries += 1
                    log(f"           -> 可修正，请模型重选坐标"
                        f"（剩 {retry_left - 1} 次机会）")
                    # 回灌错误：下一次调用把 hint 拼进 payload
                    payload = {
                        **payload,
                        "retry_hint": (
                            f"你上一次给的坐标不合法：{str(exc)[:200]}\n"
                            "**必须**从下面「当前可点项」里重新选一个坐标，"
                            "不要用任何记忆里的旧坐标。"),
                    }
                    try:
                        ans = answer_fn(payload)
                        log("           -> 重试成功")
                    except DecisionError as exc2:
                        log(f"           ⚠️ 重试仍失败：{str(exc2)[:120]}")
                        ans = {"step_done": False, "give_up": True,
                               "give_up_reason":
                                   f"连续 {retries} 次给出不合法坐标：{str(exc2)[:140]}"}
                else:
                    log("           -> 重试次数用尽，判 give_up 收尾（不猜坐标）")
                    ans = {"step_done": False, "give_up": True,
                           "give_up_reason":
                               f"模型反复给出不合法坐标：{str(exc)[:160]}"}
            else:
                ans = {"passed": None, "unknown": True,
                       "actual": f"模型回复无法解析：{str(exc)[:160]}",
                       "evidence": "（格式错误，未做判定）",
                       "event_id": "", "confidence": "low",
                       "error": str(exc)[:400]}

        # 把决定也记下来 —— 这是定位死循环的关键
        if kind == "decide":
            if ans.get("step_done"):
                log(f"           -> 判定本步完成")
            elif ans.get("give_up"):
                log(f"           -> 放弃：{ans.get('give_up_reason')}")
            elif ans.get("next_x") is not None:
                # ★ 死循环**硬阻断 + 升级**。
                # 早先只打告警、把 history 喂回去，属于"请求"模型改；
                # 实测它照样连点 9 次。纪律要由代码兜底。
                # 第一次作废决定；模型还耍赖就直接判失败收场 ——
                # 否则会空转到轮次上限，最后给个 BLOCKED（看起来像环境问题，
                # 其实是模型卡住了，结论会被误读）。
                key = f"({ans.get('next_x')},{ans.get('next_y')})"

                # ★★ 点掉弹窗**有限度地**不算连点（用户确认的处理方式）。
                #
                # 场景：某步的目标被一个临时弹窗挡住，模型点"知道了"把它清掉，
                # 然后要重新点原来的目标。这时目标坐标和之前一样，
                # 但它**不是无效重复** —— 中间有个有效的副作用（弹窗没了）。
                # 照算的话，反复弹的弹窗会把整条用例误杀成"卡死"。
                #
                # 判据只看模型自己申报的 is_dialog（它声明了点的是挡路弹窗），
                # 代码不替它猜"这算不算弹窗"。
                #
                # ⚠️ 但这个豁免必须**有上限**，否则它就是个无限通行证。
                # 真机 168 实测：模型在「取消」和「前往设置」之间来回点了
                # 7 轮，每轮都申报 is_dialog → 计数每轮被清零 → 交替循环检测
                # 永远看不到 3 个点 → 一路点到轮次上限 80，整条用例 BLOCKED。
                #
                # 所以：同一屏上连续申报弹窗的次数用完就不再豁免。
                # 正常清弹窗 1~2 次就够，给到 4 次已经很宽松。
                _DFL = 4
                if ans.get("is_dialog"):
                    if dialog_exempt.get("n", 0) < _DFL:
                        dialog_exempt["n"] = dialog_exempt.get("n", 0) + 1
                        if recent:
                            log(f"           💬 这是点掉弹窗，重置连点计数"
                                f"（之前 {len(recent)} 次不算数；"
                                f"本屏已用 {dialog_exempt['n']}/{_DFL} 次额度）")
                        recent.clear()
                        loop_blocks.clear()
                    else:
                        log(f"           ⚠️ 本屏已连续申报弹窗 {_DFL} 次，"
                            f"不再豁免 —— 按普通动作计入连点/循环检测")
                else:
                    # 没申报弹窗 = 真在推进，额度还回去
                    dialog_exempt["n"] = 0

                repeated = len(recent) >= 2 and recent[-1] == key \
                    and recent[-2] == key

                # ★ 循环不止"同一点连点"一种。
                # 真机实测（168）：模型在 (337,203) 和 (1244,1276) 之间
                # **来回弹** —— A→B→A→B 点了 58 轮、烧掉 6 分钟。
                # 原来只判"连续 3 次同一个点"，交替循环完全漏掉。
                # 这里补一个**周期检测**：最近 N 个目标若由 ≤3 个点构成、
                # 且每个都出现过至少 3 次，就是死循环。
                cycle = None
                if len(recent) >= 6:
                    window = recent[-9:]
                    uniq = set(window)
                    if 1 < len(uniq) <= 3 and all(
                            window.count(u) >= 3 for u in uniq):
                        cycle = uniq

                if cycle is not None:
                    shown = " → ".join(recent[-6:])
                    log(f"           🚫 检测到**交替死循环**：{shown}")
                    log(f"              只在这几个目标间来回点，"
                        f"说明这条路径走不通，终止本用例")
                    ans = {"step_done": False, "give_up": True,
                           "give_up_reason":
                               f"在 {sorted(cycle)} 之间交替点击无进展"
                               "（代码判定死循环）"}

                if repeated:
                    loop_blocks[key] = loop_blocks.get(key, 0) + 1
                    if loop_blocks[key] >= 2:
                        log(f"           🚫 已连续点 {key} 且拒绝后仍不改，"
                            f"判定卡死，终止本用例")
                        ans = {"step_done": False, "give_up": True,
                               "give_up_reason":
                                   f"模型连续点 {key} 无效且拒绝换目标，"
                                   "判定为卡死（代码强制终止）"}
                    else:
                        log(f"           🚫 已连续点 {key} 3 次，本决定作废"
                            f"（要求换目标）")
                        recent.append(key)
                        # ★ 光说"必须换目标"不够 —— 实测模型会**只换坐标、不换前提**：
                        # 169 里它认定"intent_back_button 是唯一返回控件"，
                        # 于是点它 → 无效 → 点闪光灯 → 又点它 → ...
                        # 前提错了，换多少坐标都没用。
                        # 所以这里把"最可能有效的另一类手段"直接摆出来：
                        # 按系统返回键离开当前页面，比在陌生 App 里猜图标可靠。
                        state = _drive(app, Command(resume={
                            "step_done": False, "give_up": False,
                            "next_x": None, "next_y": None,
                            "why": f"代码作废：连续 3 次点 {key} 无效",
                            "note": ("loop_blocked。⚠️ 换目标≠只换坐标："
                                     "如果你对『这一屏该点哪个控件』的**前提本身**"
                                     "就可能错了（例如这是别的 App 的页面、"
                                     "或这些控件里根本没有离开当前页的入口），"
                                     "请改用 key=\"back\" 退回上一层，"
                                     "而不是继续在本屏找别的图标。"),
                        }), cfg, node_calls)
                        continue
                if ans.get("give_up"):
                    log(f"           -> 放弃：{ans.get('give_up_reason')}")
                else:
                    log(f"           -> 点 ({ans.get('next_x')},"
                        f"{ans.get('next_y')}) {ans.get('why')}")
                    # 副作用当场打出来 —— 报告里有，但实时日志也要能看见，
                    # 否则跑的时候不知道它悄悄授了什么权。
                    if ans.get("grants_permission"):
                        log(f"           🔑 授权："
                            f"{ans.get('permission_name') or '未指明'} "
                            f"= {ans.get('permission_decision') or 'allow'}")
                    if ans.get("is_dialog"):
                        # 一眼分清是哪一种弹窗（用户要求）。
                        # 判据由代码给：按包名，系统权限框的包名是固定的。
                        # 只有系统权限框需要特殊处理；App 自己的权限页
                        # 是业务流程的一部分，不会自己消失，照常走。
                        _pkg = str(state.get("last_package") or "")
                        if is_system_permission_pkg(_pkg):
                            log(f"           🔐 系统权限弹窗"
                                f"「{ans.get('dialog_text') or '(未说明)'}」"
                                f"  包名={_pkg}  ← 系统框，不是 App 弹窗")
                        else:
                            log(f"           💬 App 弹窗"
                                f"「{ans.get('dialog_text') or '(未说明)'}」")
                    recent.append(key)
            else:
                # ⚠️ 这里以前一律打 "无动作"，把**五种完全不同的情况**混成一句。
                #
                # 每一次都付出了代价：
                #   · 176：模型明明给了 input_rid，日志却说"无动作"，
                #     看着像模型没说，其实是路由吞了。
                #   · 167：模型连续 16 轮说 vision_question，
                #     全被记成"无动作"。排查时我一直以为模型在乱点，
                #     实际是它在**反复求图**——日志把最重要的信号藏了。
                #
                # 教训：这一行是排查的第一现场，**它说什么模型就会以为是什么**。
                #       凡是"模型给了合法信号、但不是坐标"的分支，都必须单独说。
                if str(ans.get("input_rid") or "").strip():
                    log(f"           -> 输入 {ans.get('input_rid')}"
                        f" ⇐「{ans.get('input_text')}」")
                elif ans.get("key"):
                    log(f"           -> 按 {ans.get('key')} 键")
                elif ans.get("need_knowledge"):
                    log(f"           -> 要查知识：{ans.get('need_knowledge')}")
                elif ans.get("vision_question"):
                    log(f"           -> 要看图：{ans.get('vision_question')}")
                else:
                    log(f"           -> 无动作（模型既没给坐标、也没给"
                        f"输入/按键/知识/看图/完成）：{ans}")
        elif kind == "assert":
            # ★ unknown 不能打成 FAIL —— 那是替产品下结论。
            # 日志是主 Agent/人第一时间看的，写错方向会把"没验成"
            # 误读成"功能坏了"。三种情况分开写。
            if ans.get("unknown"):
                log(f"           -> 判不了（模型回复无法解析，本条记 BLOCKED）")
            else:
                log(f"           -> {'PASS' if ans.get('passed') else 'FAIL'}")

        state = _drive(app, Command(resume=ans), cfg, node_calls)

    if loops >= max_loops:
        log(f"⚠️ 达到轮次上限 {max_loops}，强制收尾")

    if report_path.exists():
        report = SubagentReport(**json.loads(report_path.read_text(encoding="utf-8")))
    else:
        report = SubagentReport(
            case_id=spec.case_id, status="BLOCKED",
            summary="未产出报告（执行异常）", session_id=sid)

    # ── 未采纳的判据字段：必须进报告 ──────────────────────────────
    #
    # 加载器一直在 stdout 打 `expect.xxx 未实现（未参与判定）`，
    # 但主 Agent 读的是报告 —— 只 print 等于没有。
    # 这些是"用例写了判据、实际没验"的**静默缺口**，必须让它看见。
    if yaml_warnings:
        _unused = [w for w in yaml_warnings if "expect." in w or "未实现" in w]
        report.yaml_unused_fields = list(yaml_warnings)
        if _unused:
            report.issues = list(report.issues) + [
                f"⚠️ 判据未生效：{w}" for w in _unused]
            if verbose:
                log("")
                log(f"⚠️ 有 {len(_unused)} 条判据写了但没生效"
                    f"（这些预期实际上没验）：")
                for w in _unused[:8]:
                    log(f"     · {w}")
                if len(_unused) > 8:
                    log(f"     … 另有 {len(_unused) - 8} 条，见报告")

    # ── 回写 YAML：把这次**实际走的路径**沉淀回去 ──────────────────
    #
    # 用户要求（原话）：
    #   「用我们的工具完成后要生成或者把旧的YAML不合理的修改。」
    #   「如果这个Case 第一次跑完了会生成YAML。」
    #
    # 两种情况分开处理：
    #   · 本来就有 YAML -> 回写（只改和实际不一致的地方）
    #   · 本来没有 YAML -> **首次生成**（这是这次跑出来的唯一成果，值得沉淀）
    #
    # 只在 PASS/FAIL 时写：
    #   PASS/FAIL = 路径走完了，实际怎么走到的有参考价值
    #   BLOCKED = 路径没走完，写进去会把半截路径固化成"经验"
    #
    # 失败不影响结论 —— 回写/生成是附带产物，不该让一次成功的执行变 BLOCKED。
    if writeback and not case_yaml_path and report.status in ("PASS", "FAIL"):
        # ★ 首次生成：这次跑之前没有 YAML，跑完了就把它沉淀下来。
        try:
            from ..report import generate as ygen
            _out = Path(workdir or ".") / f"{spec.case_id}.yaml"
            if not _out.parent.exists():
                from ..cases.paths import cases_dir
                _out = cases_dir() / f"{spec.case_id}.yaml"
            ygen.write_case_yaml(
                spec, _out,
                trace=report.action_trace or [],
                pitfalls=getattr(report, "pitfalls", None),
                status=report.status)
            report.yaml_writeback = {"generated": str(_out),
                                     "path": str(_out)}
            if verbose:
                log("")
                log(f"📝 首次执行完成，已生成用例 YAML：{_out.name}")
                log("   （只记结构与判据，不含任何坐标）")
        except Exception as exc:  # noqa: BLE001
            report.yaml_writeback = {"generated": "", "error": str(exc)}
            if verbose:
                log(f"⚠️ 生成 YAML 失败（不影响本次结论）：{exc}")

    elif writeback and case_yaml_path:
        try:
            from ..report import writeback as ywb
            _trace = report.action_trace or []
            _chg = ywb.diff_steps(spec, _trace)
            if report.status not in ("PASS", "FAIL"):
                res = {"changed": [], "skipped": [],
                       "path": str(case_yaml_path),
                       "note": f"结论 {report.status}，路径没走完，未回写"}
            else:
                res = ywb.writeback(case_yaml_path, _chg)
            report.yaml_writeback = res
            if verbose and res.get("changed"):
                log("")
                log(f"📝 已按本次实际路径回写 YAML：{Path(str(case_yaml_path)).name}")
                for c in res["changed"]:
                    for ln in ywb.render_change_lines(c):
                        log(f"     {ln}")
            # ⚠️ 没有差异时**什么都不打**。
            #
            # 早先这里有个 elif 打「YAML 未回写：无可写入的差异」。用户明确说不要：
            # 没差异是**最正常的结果**（用例 YAML 本来就写对了），
            # 给正常结果配一条提示，只会让人以为出了问题、
            # 还得反过来确认"是不是没写成功"。
            # 真正需要知道的是"有差异但没写成"—— 那种走下面的 except。
        except Exception as exc:  # noqa: BLE001
            report.yaml_writeback = {"changed": [], "skipped": [],
                                     "path": str(case_yaml_path),
                                     "error": str(exc)}
            if verbose:
                log(f"⚠️ YAML 回写失败（不影响本次结论）：{exc}")

    if verbose:
        log("")
        log(f"结论：{report.status}  步骤 {report.steps_done}/{len(spec.steps)}  "
            f"预期 {len(report.findings)}/{len(spec.asserts)}")
        # ⚠️ 原来这行打的是 counts（只统计会 interrupt 的节点），
        #    vision/knowledge 永远是 0 —— 而它们其实跑过。
        #    167 排查时我照着这个 0 去翻路由和图定义，白花一轮。
        #    现在主轴换成 node_calls（stream 里数出来的真实执行），
        #    counts 退成括号里的补充信息，并且明确标注它的口径。
        log(f"轮次：{loops}")
        log(f"节点执行：{_fmt_node_calls(node_calls)}")
        # model_s 在这里**才**能算准：report 里的值来自 decide 节点，
        # 而 decide 节点只发 interrupt（实测 27 次共 353ms）。
        # 真正等模型的时长是 model_ms（主循环里掐的），所以以它为准回填。
        report.model_s = round(model_ms / 1000.0, 1)
        _mm = report.model_s
        _total_s = report.elapsed_s or 0.0
        if _total_s > 0:
            log(f"⏱️  耗时：总计 {_total_s:.1f}s，其中模型 {_mm:.1f}s"
                f"（{_mm / _total_s:.0%}），设备/其他 {_total_s - _mm:.1f}s")
        else:
            log(f"⏱️  模型耗时：{_mm:.1f}s")
        log(f"    （括号：会 interrupt 的节点计数 "
            f"{counts}，不含 observe/act/vision 等不 interrupt 的节点）")

        # ★ 强证据直点到底生效没有 —— 必须**正面说清**，不能靠"日志里没看到 [model]"
        #   去反推。
        #
        #   代码直点时**本来就不该调模型**，所以没有 decide 日志是正常且正确的；
        #   但"没有日志"和"没生效"从外部看一模一样。
        #   这里把 strong_stats 明明白白打出来：
        #     by_code  = 代码直点的步数（不花 token）
        #     by_model = 落回模型的步数（定不下来才发生）
        _ss = report.strong_stats or {}
        _bc = _ss.get("by_code", 0)
        _bm = _ss.get("by_model", 0)
        log("")
        log(f"🎯 强证据：代码直点 {_bc} 次 / 交给模型 {_bm} 次"
            + (f"（自动化率 {_ss.get('auto_rate', 0.0):.0%}）" if (_bc or _bm) else ""))
        for _r in (_ss.get("fallback_reasons") or [])[:4]:
            log(f"   · 交给模型原因：{str(_r)[:110]}")

        # ★ 用例可信度（用例 YAML 的 confidence）—— **只提示，不参与判定**。
        # 为什么值得提示：level=single-run/flaky 的用例，这次 PASS
        # 不能当成"功能稳定"的证据。主 Agent 拿这个结论去做判断时该知道。
        _conf = report.case_confidence or {}
        if _conf.get("level") and _conf["level"] != "verified":
            log("")
            log(f"⚠️ 用例可信度：{_conf['level']}"
                + (f" —— {_conf['reason'][:120]}" if _conf.get("reason") else ""))
        if _conf.get("todo"):
            log(f"📌 待确认 {len(_conf['todo'])} 条：")
            for _t in _conf["todo"]:
                log(f"     · {_t}")
        # 授权/弹窗汇总
        if report.runtime_permissions:
            log("")
            log(f"🔐 Android 运行时权限：工具层自动授予 "
                f"{len(report.runtime_permissions)} 次点击")
            # 按弹窗文案归并，让人看清"到底给了哪几个权限"
            for p in report.runtime_permission_prompts:
                log(f"     · {p.get('prompt')}")
            log(f"   （按钮均为「允许」，默认 grant；"
                f"{len(report.runtime_permission_prompts)} 个权限）")
        if report.permissions_granted:
            log("")
            log(f"🔑 本次执行授权了 {len(report.permissions_granted)} 项：")
            for p in report.permissions_granted:
                log(f"     - {p.get('permission')}"
                    f"（{p.get('decision')}）"
                    f" 坐标({p.get('x')},{p.get('y')})"
                    f"  {str(p.get('why') or '')[:60]}")
        if report.dialogs_dismissed:
            log(f"💬 点掉的 App 内弹窗 {len(report.dialogs_dismissed)} 个：")
            for d in report.dialogs_dismissed:
                log(f"     - {d.get('text') or '(未说明)'}"
                    f"  坐标({d.get('x')},{d.get('y')})")

        # ★ 点击账本 —— 排查问题时先看这一段。
        #
        # 为什么值得打进日志：用户排障时最常问的就是"它到底点了什么、
        # 为什么点这个、点完有没有反应"。光看 `-> 点 (1795,1583)`
        # 是答不出来的 —— 那是像素，不是控件。
        _lg = getattr(dev, "ledger", None)
        if _lg is not None and _lg.rows():
            log("")
            log(f"🖱️ 点击账本（共 {len(_lg.rows())} 次）：")
            for _ln in _lg.render().splitlines():
                log("   " + _ln)
            log(f"   完整记录：{ledger_path}")
        log(f"报告：{report_path}")
        log(f"日志：{log_path}")
    log.close()

    # ★ 重新落一次报告文件。
    #
    # 报告是 finish_child_node 写的，那时还不知道 model_s；
    # 这里回填之后必须再写一遍，否则 run_case_json.py 从文件读到的还是 0.0，
    # 库里 model_s 就永远是 0 —— "统计了但没人看到"，等于没做。
    try:
        Path(report_path).write_text(
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass

    report_path_attr = report_path
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return report


def run_case_sync(spec_path: str, **kw) -> SubagentReport:
    """从 json 文件加载用例并执行（CLI 友好）。"""
    return run_case(CaseSpec.from_json(spec_path), **kw)
