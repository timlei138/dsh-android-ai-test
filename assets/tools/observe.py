"""observe：看一眼。dump UI 树 + 截图 + 前台状态，一次全给，证据落盘。

用法：
  python observe.py --name case172/step01 [--serial xxx] [--dir storage/evidence]
"""
from __future__ import annotations

import argparse

from common import (add_common_args, backfill_package, capture, connect, emit,
                    evidence_paths, fail, guard_terminated, log_auto, next_targets, labeled_nodes, npm_identity,
                    rel, summarize_xml)
import time
from datetime import datetime
from runtime import timed_call, execution


def main() -> None:
    p = argparse.ArgumentParser(description="observe：dump + 截图 + 前台状态")
    add_common_args(p)
    args = p.parse_args()

    guard_terminated("observe")   # 会话已因崩溃终止 → 拒绝执行

    try:
        d = connect(args.serial)
    except Exception as e:  # noqa: BLE001
        fail(f"设备连接失败: {e}")

    png, xml, meta = evidence_paths(args.dir, args.name)
    t0 = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="milliseconds")
    execution("may_have_executed")

    # 权限弹窗：按会话级意图自动响应（在截图前处理，弹窗约 6s 会自行消失）
    perm_result = None
    try:
        from db import current_session, get_db
        si = current_session()
        intent = get_db().get_perm_intent(si) if si else None
        import perm as _perm
        # ⚠️ observe 是"观察时顺手遇到的权限框"，此刻前台**已经是权限框本身**
        # （com.android.permissioncontroller），拿 app_current 会把框自己的包名
        # 当成申请方 —— 那是错的归属，比不记更糟。
        # 所以 observe 这里**不传归属**，只留 prompts 文案；
        # 真正的归属由 act 那条路径记（动作前的前台才是申请方）。
        perm_result = timed_call("permission", _perm.handle,
                                 d, intent["action"] if intent else None)
    except Exception as e:  # noqa: BLE001
        perm_result = {"error": str(e)}

    try:
        info = capture(d, png, xml, meta)
    except Exception as e:  # noqa: BLE001
        fail(f"采集失败: {e}")
    execution("executed")
    detail = "观察屏幕"
    if perm_result and perm_result.get("detected"):
        clk = ", ".join(c["text"] for c in perm_result.get("clicks") or [])
        detail += (f" · 权限弹窗({perm_result.get('activity')})"
                   + (f" 已自动点击: {clk}" if clk else " 未点击"))
    # 和 act 一样带**屏幕身份签名** —— 自动整理路径边时要靠它判"这一步在哪个节点"。
    # ⚠️ observe 也必须带：会话开头往往先 observe（还没有 act），
    # 若它没签名，第一条边的前溯就落空，源节点只能退回裸 activity（真实缺陷）。
    info = {**info, "sig": npm_identity(
        xml.read_text(encoding="utf-8"),
        None if getattr(args, "no_filter", False) else info.get("package"))}

    # 崩溃检测：observe 也必须做。
    # ⚠️ 真实事故：App 启动即崩时 AI 只会反复 observe（还没机会 act），
    # 若崩溃检测只挂在 act 上，这个最该被抓到的崩溃反而全程漏抓。
    alert = None
    terminate = False
    try:
        from act import check_crash
        crash = check_crash(d, info.get("package"), png.parent)
    except Exception:  # noqa: BLE001
        crash = None
    if crash and crash.get("new"):
        from logcat import verdict as crash_verdict
        v = crash_verdict(crash)
        entry = crash.get("entry") or {}
        scope = crash.get("scope")
        info["crash_detected"] = True
        info["crash"] = {"scope": scope, "type": entry.get("type"),
                         "process": entry.get("process"), "time": entry.get("time"),
                         "log_path": crash.get("log_path")}
        if v["blocked"]:
            alert = v["message"]
            terminate = True
            info["blocked"] = True
            # 写会话终态：后续 act/observe/read 会被工具层直接拒绝（硬拦截）
            try:
                from db import current_session, get_db
                sid = current_session()
                if sid:
                    get_db().terminate_session(sid, v["message"])
            except Exception:  # noqa: BLE001
                pass
    duration_ms = round((time.perf_counter() - t0) * 1000)
    info["duration_scope"] = "core operation through capture/signature/crash check; excludes connection, event write and response"
    log_auto("observe", "observe", detail, info, rel(png), duration_ms, started_at)
    if crash and crash.get("new"):
        # 把 crash 标记载入事件流，使 finish 的 crash_events 计数正确
        log_auto("crash", "observe crash",
                 f"⚠️{v['message']}",
                 info, crash.get("log_path"))

    backfill_package(info.get("package"))
    nodes = summarize_xml(xml.read_text(encoding="utf-8"),
                          mode="full" if getattr(args, "full", False) else "nav",
                          fg_package=None if getattr(args, "no_filter", False)
                          else info.get("package"))
    # 知识卡浮现：`start` 时若还不知道包名，由首个 observe 补上（只提示一次）
    k_hint = None
    try:
        from common import knowledge_hint
        from db import current_session, get_db
        _sid = current_session()
        if _sid and not get_db().knowledge_shown(_sid):
            k_hint = knowledge_hint(info.get("package"),
                                    screen=info.get("activity"))
            # ⚠️ 条件必须是"发出了 hint"而非"有专属卡"：无卡包返回的也是非 None
            # （card=None + system_card 提示），若按 card 判就永远不落标记，
            # 导致**每次 observe 都重复带一遍 system 提示**（实测确认）。
            if k_hint:
                get_db().mark_knowledge_shown(_sid)
    except Exception:  # noqa: BLE001
        k_hint = None
    payload = {
        **info,
        "evidence": {"png": str(png), "xml": str(xml), "meta": str(meta)},
        "next": next_targets(nodes, labeled=labeled_nodes(   # 精简可点清单（与 act 一致）
            xml.read_text(encoding="utf-8"),
            None if getattr(args, "no_filter", False) else info.get("package"))),
        "nodes": nodes,
        "nodes_count": len(nodes),
        "nodes_mode": "full" if getattr(args, "full", False) else "nav",
        "hint": ("定位降级链：r/rid > d/desc > t/text > c/center（坐标本次 dump 现场派生）。"
                 "`next` 是精简可点清单；要按页面文字做断言时加 --full"),
    }
    if k_hint:
        payload["knowledge_hint"] = k_hint
    if alert:
        payload["alert"] = alert
    if perm_result and perm_result.get("detected"):
        payload["permission"] = perm_result
        if not (perm_result.get("clicks") or []):
            payload["permission_hint"] = (
                "检测到系统权限弹窗但**没有自动点击**：弹窗里没有可匹配的按钮"
                f"（可见 {perm_result.get('buttons')}）。"
                "若是要测「拒绝」路径，用 perm-intent --action deny 后重触发；"
                "否则看 buttons 自己决定点哪个。")
    if terminate:
        # 硬性终止信号：AI 必须立即 finish(BLOCKED)，不要再发下一个动作
        payload["terminate"] = True
        payload["terminate_reason"] = alert
        payload["hint"] = ("⛔ 检测到被测/关联包崩溃 → 立即停止执行："
                           "session.py finding --status BLOCKED ... 然后 "
                           "session.py finish --status BLOCKED ...；不要继续下一步")
    emit(True, payload)


if __name__ == "__main__":
    main()
