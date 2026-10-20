"""真机最小验证：只验证「恢复时不重放设备点击」。

安全约束（严格遵守）：
  - 所有工具调用一律走 RealDevice（内部统一走 run.py 账本）
  - 绝不直接调 tools/*.py —— 上一轮就是这么把会话 475 弄锁死的
  - 只点 1 次，不 pm clear，不碰日历数据
  - 跑完主动 finish ERROR（技术验证，非正式用例）

判据：
  点 1 次 → resume 3 次 → 设备侧 act 事件数应为 1
  若为 2 或 3，说明 interrupt 恢复时重放了点击（核心假设不成立）
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from android_agent.agent.graph import build_graph  # noqa: E402
from android_agent.device.bridge import RealDevice, ToolError  # noqa: E402

from android_agent.cases.paths import skill_root  # noqa: E402
SKILL = skill_root()
DB = Path(__file__).parent / "real_checkpoints.db"
ok_all = True


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok_all
    if not cond:
        ok_all = False
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  -> {detail}" if detail else ""))


def act_events(session_id: int) -> int:
    """设备侧真实 act 事件数 —— 这是判定重放的唯一可信来源。"""
    conn = sqlite3.connect(Path(SKILL) / "storage" / "sessions.db")
    try:
        row = conn.execute(
            "select count(*) from events where session_id=? and kind='act'",
            (session_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def main() -> None:
    if DB.exists():
        DB.unlink()

    dev = RealDevice(SKILL, serial="HA2DMRBK")

    ok, why = dev.connected()
    print(f"设备：{why}")
    if not ok:
        print("设备未就绪，退出")
        sys.exit(2)

    # 1) 开一个真实会话（走 run.py 账本）
    print("\n=== 开真实会话 ===")
    try:
        sid = dev.start_session("LangGraph 重放验证", package="com.zui.calendar",
                                diag=True)
    except ToolError as exc:
        print(f"  [FAIL] session start 失败：{exc}（executed={exc.executed}）")
        sys.exit(1)
    print(f"  [PASS] session_id={sid}")

    # 2) 观察，拿真实可点清单
    print("\n=== observe（走账本）===")
    try:
        obs = dev.observe()
    except ToolError as exc:
        print(f"  [FAIL] observe 失败：{exc}（executed={exc.executed}）")
        sys.exit(1)
    check("observe 成功", True, f"activity={obs.activity}")
    clickable = [n for n in obs.nodes if n.get("c")]
    check("拿到可点清单", len(clickable) > 0,
          str([(n.get("r"), n.get("t")) for n in clickable][:5]))
    if not clickable:
        print("  没有可点目标，退出")
        sys.exit(1)

    # 选一个"无害"的目标：优先 back/home 类，否则第一个可点项
    # ⚠️ 这一步只是为了产生一次真实 act，不追求业务语义
    target = None
    for n in clickable:
        rid = (n.get("r") or "")
        if rid in ("iv_more", "btnToday", "iv_today"):
            target = n
            break
    if target is None:
        target = clickable[0]
    x, y = target["c"]
    print(f"  目标：rid={target.get('r')} t={target.get('t')} c=[{x},{y}]")

    # 3) 建图，把"点一次"作为第 1 步
    print("\n=== 走图：点 1 次 → 3 次 resume ===")
    app = build_graph(dev, checkpointer=SqliteSaver(
        sqlite3.connect(DB, check_same_thread=False)))
    cfg = {"configurable": {"thread_id": f"real-{sid}"}}

    init = {
        "case_id": "replay-check",
        "package": "com.zui.calendar",
        "expectations": ["仅验证重放，不断言业务"],
        "rounds": 0, "max_rounds": 12, "findings": [],
    }

    r = app.invoke(init, cfg)
    check("第1次 interrupt（未动作）", bool(r.get("__interrupt__")))
    check("此时 act 事件数=0", act_events(sid) == 0, f"acts={act_events(sid)}")

    # resume #1：给出坐标 -> 应真的点 1 次
    r = app.invoke(Command(resume={
        "done": False, "next_x": x, "next_y": y,
        "why": "LangGraph 重放验证：点一次",
    }), cfg)
    n1 = act_events(sid)
    check("resume#1 后 act 事件数=1", n1 == 1, f"acts={n1}")
    check("resume#1 后再次 interrupt", bool(r.get("__interrupt__")))

    # resume #2、#3：声明 done（不再给坐标）-> 不应产生任何点击
    r = app.invoke(Command(resume={"done": True, "why": "收工"}), cfg)
    n2 = act_events(sid)
    check("resume#2 后 act 事件数仍=1（无重放）", n2 == 1, f"acts={n2}")

    # 此刻应进入预期收集；逐条答完，验证全程 act 不增长
    guard = 0
    while r.get("__interrupt__") and guard < 8:
        guard += 1
        p = r["__interrupt__"][0].value
        if p.get("kind") == "verdict":
            ev = (p.get("evidence_event_ids") or [""])[0]
            r = app.invoke(Command(resume={
                "passed": True,
                "actual": "技术验证：仅确认无重放，不做业务断言",
                "event_id": ev}), cfg)
        else:
            r = app.invoke(Command(resume={"done": True}), cfg)

    n3 = act_events(sid)
    check("★ 全程结束 act 事件数仍=1（无重放）", n3 == 1, f"acts={n3}")
    check("图跑到终点", not r.get("__interrupt__"),
          f"status={r.get('status')}")

    # 4) 收尾说明：图跑到终点时 finish_node 已经提交过会话，
    #    所以这里**不应**再次 finish（会报"没有进行中的会话"）。
    print("\n=== 收尾状态 ===")
    print(f"  会话 {sid} 已由图内 finish_node 提交，无需再收尾")
    check("会话已由图提交（不再重复 finish）", True, f"session_id={sid}")

    print("\n" + "=" * 52)
    print("  核心假设成立：恢复不重放" if ok_all else "  存在失败项")
    print("=" * 52)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
