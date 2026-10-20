"""LangGraph 执行骨架：Android 用例执行的状态机。

设计要点（两条硬约束，直接来自 interrupt 的官方语义）：

1. act 与 judge 必须是【两个节点】。
   interrupt() 恢复时，「整个节点会从头再跑一遍」。而 act 会真的点设备 ——
   若把 act 和 interrupt 放在同一节点，第 N 次恢复会把前 N-1 次点击全部重放。
   => act_node 负责"动"，judge_node 负责"问 AI"，职责物理隔离。

2. 逐步循环用【条件边回环】，不用单节点内 while + interrupt。
   官方明确：节点内循环 interrupt 会因索引重放导致执行次数指数增长。
   => route_after_judge 决定回 act_node 还是去 finish。

AI 介入点只有一个：judge_node 里的 interrupt()。
流程推进、纪律约束都在代码里 —— 这正是"AI 只在需要判断的节点介入"。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from ..device.bridge import Device, MockDevice


# --------------------------------------------------------------------------
# AI 判断的结构化契约
# interrupt(response_schema=...) 会校验 AI 的返回值，
# 模型乱答会被 pydantic 挡下来，而不是让脏数据流进状态机。
# --------------------------------------------------------------------------

class StepDecision(BaseModel):
    """AI 对"当前这步"的判断。"""
    done: bool = Field(description="用例的全部步骤是否已走完")
    next_x: int | None = Field(default=None, description="下一步点击的 x 坐标")
    next_y: int | None = Field(default=None, description="下一步点击的 y 坐标")
    why: str = Field(default="", description="这一步在做什么（写进执行记录）")
    expect: str | None = Field(default=None, description="本步落点判据，如 activity=XxxActivity")
    note: str = Field(default="", description="观察到的情况")


class ExpectVerdict(BaseModel):
    """AI 对"某条预期是否达成"的判断。逐条来。"""
    passed: bool = Field(description="该条预期是否达成")
    actual: str = Field(description="实际看到的现象（必须具体，不能只写'正常'）")
    event_id: str = Field(description="绑定的证据事件 ID")


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------

class CaseState(TypedDict, total=False):
    # 输入
    case_id: str
    package: str
    expectations: list[str]          # 用例写了几条预期

    # 运行时
    rounds: int
    max_rounds: int
    last_activity: str
    next_nodes: list[dict[str, Any]]
    last_event_ids: list[str]        # 最近一次 act 的证据 ID（持久保存，供 finding 绑定）
    pending_decision: dict[str, Any] | None

    # 断言：用 operator.add 累积，逐条追加
    findings: Annotated[list[dict[str, Any]], operator.add]

    # 收尾
    status: str
    summary: str
    self_check: dict[str, Any]


# --------------------------------------------------------------------------
# 设备实例注入
# 真实场景由外部 set_device() 换成 RealDevice，图定义不变。
# --------------------------------------------------------------------------

_DEVICE: Device | None = None


def set_device(device: Device) -> None:
    global _DEVICE
    _DEVICE = device


def _dev() -> Device:
    if _DEVICE is None:
        raise RuntimeError("设备未注入：先调用 set_device(MockDevice() | RealDevice(...))")
    return _DEVICE


# --------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------

def act_node(state: CaseState) -> dict[str, Any]:
    """执行一步动作。【不含 interrupt】—— 保证恢复时不会重复点设备。

    两种入口：
      - 首轮 / AI 给了新坐标 -> 执行点击
      - AI 判定 done 或未给坐标 -> 空过，交给 judge 决定去留
    """
    decision = state.get("pending_decision") or {}

    # AI 说做完了，或没给坐标 -> 本节点不产生任何设备动作
    if decision.get("done") or decision.get("next_x") is None:
        obs = _dev().observe()
        return {
            "last_activity": obs.activity,
            "next_nodes": obs.nodes,
            "rounds": state.get("rounds", 0),
            "findings": [],
        }

    res = _dev().act(
        x=int(decision["next_x"]),
        y=int(decision["next_y"]),
        why=str(decision.get("why", "")),
        expect=decision.get("expect"),
    )

    if not res.ok and not res.executed:
        # 明确未碰设备 -> 允许重试，不推进轮次
        return {
            "pending_decision": None,
            "last_activity": res.activity,
            "next_nodes": [],
            "rounds": state.get("rounds", 0),
            "findings": [],
        }

    # 证据 ID 挂到决策上，供后续 finding 绑定
    ev_ids = res.event_ids
    delta: dict[str, Any] = {
        "pending_decision": {
            **decision,
            "event_ids": ev_ids,
            "checkpoint": res.checkpoint,
            "acted": True,
        },
        "last_activity": res.activity,
        "next_nodes": res.next,
        "last_event_ids": ev_ids,     # 持久保存：finding 绑证据靠它
        "rounds": state.get("rounds", 0) + 1,
        "findings": [],
    }
    return delta


def judge_node(state: CaseState) -> dict[str, Any]:
    """AI 判断点。【唯一含 interrupt 的节点】—— 只读状态，无设备副作用。

    喂给 AI 的是：当前落点 + 可点清单 + 上一步结果 + 剩余轮次。
    AI 回：继续（给坐标）或 收工（done=True）。
    """
    decision = state.get("pending_decision") or {}
    payload = {
        "kind": "next_step",
        "activity": state.get("last_activity"),
        "clickable": state.get("next_nodes", []),
        "last_action": {
            "why": decision.get("why"),
            "checkpoint": decision.get("checkpoint"),
        },
        "rounds_used": state.get("rounds", 0),
        "rounds_left": state.get("max_rounds", 12) - state.get("rounds", 0),
        "question": "下一步点哪里？若用例步骤已走完，请置 done=true。",
    }

    # ↓ interrupt 只在这里，前面没有任何设备副作用，恢复安全
    answer = interrupt(payload, response_schema=StepDecision)

    if isinstance(answer, StepDecision):
        answer = answer.model_dump()
    return {"pending_decision": answer}


def expect_node(state: CaseState) -> dict[str, Any]:
    """逐条预期收集：一次只问一条，避免"写成一坨"。

    这里同样只读状态 —— 设备副作用（finding 落库）在 verdict_node。
    """
    idx = len(state.get("findings", []))
    expects = state.get("expectations", [])

    if idx >= len(expects):
        return {"findings": []}

    # 证据 ID 来自 state 里持久保存的最近一次 act 证据。
    # 注意：不能从 pending_decision 取 —— 它在 verdict 后会被清空，
    # 导致第 2 条预期起拿不到证据（曾经的 bug）。
    ev_ids = list(state.get("last_event_ids") or [])

    payload = {
        "kind": "verdict",
        "index": idx + 1,
        "total": len(expects),
        "expect": expects[idx],
        "activity": state.get("last_activity"),
        "clickable": state.get("next_nodes", []),
        "evidence_event_ids": ev_ids,
        "question": "这条预期是否达成？actual 必须写具体现象；event_id 必须取自 evidence_event_ids。",
    }
    answer = interrupt(payload, response_schema=ExpectVerdict)
    if isinstance(answer, ExpectVerdict):
        answer = answer.model_dump()

    return {"pending_decision": {**(state.get("pending_decision") or {}), **answer}}


def verdict_node(state: CaseState) -> dict[str, Any]:
    """把 AI 的判定落成 finding。副作用集中在这里，且幂等（按索引去重）。

    ⚠️ 证据校验：event_id 必须来自本会话真实证据（evidence_event_ids），
    否则拒绝落库 —— 这正是「PASS finding 必须带 --event-id」的硬约束化。
    缺证据时该条记为 FAIL 而非静默通过。
    """
    idx = len(state.get("findings", []))
    expects = state.get("expectations", [])
    if idx >= len(expects):
        return {"findings": []}

    decision = state.get("pending_decision") or {}
    valid_ids = set(state.get("last_event_ids") or [])
    got_id = str(decision.get("event_id", ""))
    passed = bool(decision.get("passed"))
    actual = str(decision.get("actual", ""))

    issues: list[str] = []
    if not got_id:
        issues.append("缺证据 ID")
    elif valid_ids and got_id not in valid_ids:
        issues.append(f"证据 ID {got_id} 不属于本会话证据 {sorted(valid_ids)}")
    if not actual.strip():
        issues.append("actual 为空（必须写具体现象）")

    if issues:
        # 证据不成立 -> 该条不得记 PASS
        passed = False
        actual = f"{actual}｜[记录拒绝] " + "；".join(issues)

    status = "PASS" if passed else "FAIL"

    _dev().finding(
        status=status,
        event_id=got_id or "(none)",
        expect=expects[idx],
        actual=actual,
    )

    return {
        "findings": [{
            "index": idx + 1,
            "expect": expects[idx],
            "status": status,
            "actual": actual,
            "event_id": got_id,
            "rejected": bool(issues),
            "issues": issues,
        }],
        "pending_decision": None,
    }


def gate_node(state: CaseState) -> dict[str, Any]:
    """覆盖闸门（硬约束）：预期没逐条收齐，不许收尾。

    这条纪律以前写在 SKILL.md 里靠模型自觉，现在是图的结构：
    findings 数 != expectations 数 -> 不进入 finish。
    """
    expects = state.get("expectations", [])
    got = len(state.get("findings", []))
    rounds = state.get("rounds", 0)
    max_rounds = state.get("max_rounds", 12)

    blocked_reasons: list[str] = []
    if got != len(expects):
        blocked_reasons.append(f"预期 {len(expects)} 条，只验到 {got} 条")
    if rounds >= max_rounds:
        blocked_reasons.append(f"步数达到上限 {max_rounds}")

    if blocked_reasons:
        status = "BLOCKED"
        summary = "；".join(blocked_reasons)
    else:
        status = "FAIL" if any(f["status"] == "FAIL"
                               for f in state.get("findings", [])) else "PASS"
        summary = f"预期 {got}/{len(expects)} 条已验证，共 {rounds} 步"

    return {
        "status": status,
        "summary": summary,
        "self_check": {
            "expectations": len(expects),
            "findings": got,
            "rounds": rounds,
            "coverage_ok": got == len(expects),
            "blocked_reasons": blocked_reasons,
        },
        "findings": [],
    }


def finish_node(state: CaseState) -> dict[str, Any]:
    """收尾提交。只在闸门放行后到达。"""
    _dev().finish(status=state.get("status", "BLOCKED"),
                  summary=state.get("summary", ""))
    return {"findings": []}


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

def route_after_judge(state: CaseState) -> Literal["act", "expect"]:
    """AI 说 done -> 去收预期；否则回 act 再走一步。

    用条件边回环，而不是节点内 while —— 避免 interrupt 索引重放。
    """
    decision = state.get("pending_decision") or {}
    return "expect" if decision.get("done") else "act"


def route_after_verdict(state: CaseState) -> Literal["expect", "gate"]:
    """还有预期没收完就继续收。"""
    idx = len(state.get("findings", []))
    return "expect" if idx < len(state.get("expectations", [])) else "gate"


# --------------------------------------------------------------------------
# 建图
# --------------------------------------------------------------------------

def build_graph(device: Device | None = None, checkpointer: Any = None):
    if device is not None:
        set_device(device)

    b = StateGraph(CaseState)
    b.add_node("act", act_node)
    b.add_node("judge", judge_node)
    b.add_node("expect", expect_node)
    b.add_node("verdict", verdict_node)
    b.add_node("gate", gate_node)
    b.add_node("finish", finish_node)

    b.add_edge(START, "act")
    b.add_edge("act", "judge")
    b.add_conditional_edges("judge", route_after_judge,
                            {"act": "act", "expect": "expect"})
    b.add_edge("expect", "verdict")
    b.add_conditional_edges("verdict", route_after_verdict,
                            {"expect": "expect", "gate": "gate"})
    b.add_edge("gate", "finish")
    b.add_edge("finish", END)

    return b.compile(checkpointer=checkpointer)
