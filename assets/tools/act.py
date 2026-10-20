"""act：动一下。执行单个动作后自动回传动作后画面（= act + observe 一次完成）。

用法：
  python act.py tap --x 540 --y 1200 --via rid=com.app:id/btn_ok --why "提交表单" --name case172/step02
  python act.py longclick --x 540 --y 1200 --name ...
  python act.py input --x 540 --y 1200 --text "你好" --via text="确定" --name ...
  python act.py input --x 540 --y 1200 --text "追加内容" --no-click --append --name ...
  python act.py swipe --from 540,2000 --to 540,800 --dur 300 --watch text="允许" --name ...
  python act.py back --why "关闭二级页" --name ...
  python act.py home --name ...
  python act.py key --code 4 --name ...        # 4=BACK 3=HOME（一般用 back/home 即可）

语义说明：
- input 默认会先 click 目标坐标聚焦输入框（AI 已点过输入框时加 --no-click 避免
  双击副作用）；默认 clear=True 覆盖已有内容，追加用 --append。
- --watch：动作后监听瞬态弹窗并立即点击（tap/longclick/input/swipe 均支持），
  多候选用 | 分隔；决策在挂载时完成，弹窗出现亚秒级响应，不经 AI。
- --via 定位依据溯源（链路卡提炼靠它）；--why 本步理由（时间轴展示）。
- --settle 动作后稳定窗口毫秒数（默认 600）。异步渲染页（联网解析等）AI 应
  按知识卡经验加大，而不是靠反复 observe 轮询堆证据。
"""
from __future__ import annotations
from runtime import execution

import argparse
import sys
import time
from datetime import datetime
from runtime import timed_call

from common import (add_common_args, add_common_args_all_subcommands, capture,
                    next_targets, labeled_nodes, npm_identity,
                    connect, emit, evidence_paths, fail, guard_terminated,
                    log_auto, rel, summarize_xml, ROOT)
import checkpoint


def xy(v: str) -> tuple[int, int]:
    a, b = v.split(",")
    return int(a), int(b)


def parse_selector(spec: str) -> dict:
    """watch/选择器规格 → u2 查询参数。支持 rid=/text=/desc=。"""
    for prefix, key in (("rid=", "resourceId"), ("text=", "text"), ("desc=", "description")):
        if spec.startswith(prefix):
            return {key: spec[len(prefix):]}
    raise SystemExit(f"选择器规格不支持: {spec}（支持 rid=/text=/desc=）")


def check_crash(d, package: str | None, evidence_dir) -> dict | None:
    """崩溃检测（分级 + 20s 上下文取证）委托给 logcat.detect。

    ⚠️ evidence_dir 必须传当前步骤证据目录，否则 crash.log 不会落盘、
    崩溃取证就是空承诺（本注释为修过的真实事故留）。

    ⚠️ target 不能只用"当前前台包"：**被测 App 崩溃后前台会回落到桌面**，
    此时 foreground=com.zui.launcher、崩溃 process=com.zui.calendar，
    按前台包分类必然判成 other（"不影响判定"）→ 崩溃被放过。
    故优先用会话声明的被测包（首个 observe 回填的 session.package），
    前台包仅在会话未记录时兜底。
    """
    related: list[str] = []
    target = package
    try:
        from db import current_session, get_db
        sid = current_session()
        if sid:
            related = get_db().get_related(sid)
            sess_pkg = get_db().get_session_package(sid)
            if sess_pkg:
                target = sess_pkg
    except Exception:  # noqa: BLE001
        pass
    from logcat import detect as detect_crash
    return timed_call("crash_detection", detect_crash, d, target, related, evidence_dir)


def watch_and_click(d, stages: list[list[str]], timeout_ms: int,
                    per_stage_ms: int = 2500) -> dict:
    """瞬态弹窗确定性响应：在 UiAutomator 层轮询匹配即点（亚秒级）。

    用于权限框等**出现几秒就消失**的弹窗——决策在挂载时已完成，工具只负责快。

    ⚠️ 系统权限框约 6s 未点击会自动消失（Android 通用机制）。
    所以绝不能用"点一下 → observe 看看 → 再决定点哪"的节奏：
    一次 observe（截图 2.4MB + dump）就足以吃掉整个窗口（真实事故：
    168 用例的图库权限框就是这样丢的，最后权限没授上、选择器打不开）。

    支持**多阶段**：`stages=[stage1_specs, stage2_specs, ...]`
    典型场景是"App 自己的提示框 → 系统权限框"两段式：
    stage1 命中并点掉后，继续等 stage2，直到所有阶段处理完或超时。

    参数 `stages` 为二维列表（每阶段一组候选，阶段内用 | 分隔）。
    返回 {"clicked": [...每次点中的 spec...], "waited_ms":..., "missed_stage": idx}
    """
    t_start = time.time()
    all_deadline = t_start + timeout_ms / 1000
    clicked: list[dict] = []
    for idx, specs in enumerate(stages):
        selectors = [(s, parse_selector(s)) for s in specs]
        # 每阶段给一个较短的窗口；但总时长不超过 all_deadline
        stage_deadline = min(all_deadline, time.time() + per_stage_ms / 1000)
        hit = False
        while time.time() < stage_deadline:
            for spec, sel in selectors:
                try:
                    obj = d(**sel)
                    if obj.exists:
                        obj.click()
                        clicked.append({"stage": idx, "spec": spec,
                                        "waited_ms": round((time.time() - t_start) * 1000)})
                        hit = True
                        break
                except Exception:  # noqa: BLE001
                    pass
            if hit:
                break
            time.sleep(0.15)
        if not hit:
            # 该阶段没出现：可能本来就没有（如权限已授予），交给 AI 判断，不算致命
            return {"clicked": clicked, "missed_stage": idx,
                    "missed_specs": specs,
                    "waited_ms": round((time.time() - t_start) * 1000)}
        time.sleep(0.25)   # 让下一个弹窗有时间渲染
    return {"clicked": clicked, "missed_stage": None,
            "waited_ms": round((time.time() - t_start) * 1000)}


SELECTOR_FLAGS = ("--text", "--id", "--rid", "--desc", "--xpath", "--uiautomator")


def _reject_selector_misuse(p: argparse.ArgumentParser) -> None:
    """拦住最常见的误用：给 tap/longclick 传文本选择器。

    ⚠️ 这两个动作**只收坐标**，没有选择器形式 —— 坐标由本次 observe 的
    next[].c 现场派生（比选择器更稳，可规避"同 rid 多态"）。
    直接抛 argparse 默认错误（"the following arguments are required: --x, --y"）
    只会让人以为"少传了参数"，故在此给出可执行的下一步。
    """
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("tap", "longclick"):
        return
    used = [flag for flag in SELECTOR_FLAGS if flag in argv]
    if not used:
        return
    action = argv[0]
    p.error(
        f"{action} 不支持 {'/'.join(used)} —— 该动作只按坐标点击。\n"
        f"  正确做法：先 `python tools/observe.py` 取 next[].c 坐标，\n"
        f"           再 `python tools/act.py {action} --x <c0> --y <c1> --via \"rid=xxx\"`。\n"
        f"  要按文字定位请用 observe 输出的坐标，不要猜选择器。"
    )


def _first(v) -> str:
    """parse_expect 的值是**列表**（支持 `a|b`），这里取第一个用于定位。"""
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _find_anchor(nodes: list[dict], labeled: list[tuple], sel: dict) -> dict | None:
    """在当前屏幕里找一个锚点 → 返回 {x, y, label, how}。

    降级链与路径图一致：rid → desc → text。
    坐标**现场派生**（不写死），所以同一份路径图换设备/分辨率仍可用。
    """
    want_rid = _first(sel.get("rid")).split("/")[-1]
    want_desc = _first(sel.get("desc"))
    want_text = _first(sel.get("text"))

    # full 模式给的是完整键；nav 模式是短键 r/d/t
    for n in nodes:
        c = n.get("center") or n.get("c")
        if not c:
            continue
        rid = (n.get("rid") or n.get("r") or "").split("/")[-1]
        desc = n.get("desc") or n.get("d") or ""
        text = n.get("text") or n.get("t") or ""
        if want_rid and rid == want_rid:
            # 同 rid 多态：若同时给了 text，必须文字也对上（否则会点错项）
            if want_text and want_text not in text:
                # 文字可能在子节点里 → 用 nearby 文字兜底
                near = ""
                for lx, ly, lt in (labeled or []):
                    if abs(lx - c[0]) <= 400 and abs(ly - c[1]) <= 80 and len(lt) > len(near):
                        near = lt
                if want_text not in near:
                    continue
                return {"x": c[0], "y": c[1], "label": want_text, "how": f"rid={want_rid}+text"}
            return {"x": c[0], "y": c[1], "label": text or desc or want_rid,
                    "how": f"rid={want_rid}"}
    for n in nodes:
        c = n.get("center") or n.get("c")
        if not c:
            continue
        desc = n.get("desc") or n.get("d") or ""
        if want_desc and want_desc in desc:
            return {"x": c[0], "y": c[1], "label": desc, "how": f"desc={want_desc}"}
    # text 兜底：必须**整串相等**，不能用 `in`。
    # ⚠️ 用子串匹配会点到无关元素 —— 真实事故：找 `text=设置` 时命中了页面上
    # 另一个含"设置"的容器（`layout_time_settings`），点到了完全不同的行，
    # 落点跑到 TimeSlotSettingsActivity。宁可找不到（报 anchor_not_found），
    # 也不要"猜一个差不多"的（那会把测试带到错误页面还继续跑）。
    for n in nodes:
        c = n.get("center") or n.get("c")
        if not c:
            continue
        text = (n.get("text") or n.get("t") or "").strip()
        if want_text and text == want_text:
            return {"x": c[0], "y": c[1], "label": text, "how": f"text={want_text}"}
    # 再退一步：可点容器自己没有文字、文字在子节点里 → 邻近且**相等**才算
    for n in nodes:
        c = n.get("center") or n.get("c")
        if not c:
            continue
        for lx, ly, lt in (labeled or []):
            if (want_text and lt.strip() == want_text
                    and abs(lx - c[0]) <= 400 and abs(ly - c[1]) <= 80):
                return {"x": c[0], "y": c[1], "label": lt, "how": f"text≈{want_text}"}
    return None


def run_steps(args) -> None:
    """按路径图批量执行：起点 → 终点，每步校验检查点，不符即停。"""
    import pathmap
    # ⚠️ 整批必须**保证有结构化返回**：run.py 按"是否拿到结构化结果"判定
    # 记录是否完整，异常逃逸会让 ledger 停在 started、把会话锁死（真实事故）。
    try:
        _run_steps_inner(args, pathmap)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        emit(False, {"error": f"act steps 失败: {type(e).__name__}: {e}",
                     "hint": "本批未完成；已走的步骤见上面的逐步证据。"
                             "修好后用 --resume-from N 接着跑。"})


def _run_steps_inner(args, pathmap) -> None:
    pkg = getattr(args, "package", None)
    if not pkg:
        try:
            from db import current_session, get_db
            si = current_session()
            if si:
                row = get_db().get_session(si)
                pkg = (row or {}).get("package") if isinstance(row, dict) else None
        except Exception:  # noqa: BLE001
            pkg = None
    if not pkg:
        # 没有活动会话/没记包名 → 看路径图目录：只有一张图时无歧义，直接用
        maps = sorted((ROOT / "knowledge" / "paths").glob("*.md"))
        if len(maps) == 1:
            pkg = maps[0].stem
        elif maps:
            emit(False, {"error": "有多个路径图，无法判断用哪张："
                                  + ", ".join(m.stem for m in maps)
                                  + "。加 --package <包名> 指定。"})
            return
    if not pkg:
        emit(False, {"error": "找不到路径图 knowledge/paths/*.md"
                              "（act steps 依赖路径图；没有图就先手动跑一遍采边）"})
        return

    path_map = ROOT / "knowledge" / "paths" / f"{pkg}.md"
    if not path_map.is_file():
        emit(False, {"error": f"路径图不存在: knowledge/paths/{pkg}.md"})
        return
    nodes = pathmap.load(path_map)
    plan = pathmap.path_between(nodes, args.src, args.dst, args.max_hops)
    if not plan["found"]:
        emit(False, {"error": plan["reason"],
                     "hint": "路径图里没有这条通路。先手动跑一遍，"
                             "再用 session.py paths 采集边、paths-apply 写回。"})
        return
    if args.dry_run:
        emit(True, {"dry_run": True, "route": plan["nodes"],
                    "steps": [{"n": i, "from": s["from"], "to": s["to"],
                               "anchor": s["anchor"], "selector": pathmap.edge_selector(s),
                               "expect": pathmap.finish_expect(pathmap.find_node(nodes, s["to"]))}
                              for i, s in enumerate(plan["steps"], 1)]})
        return

    d = connect(args.serial)
    results = []
    start = max(1, args.resume_from)

    # 先认起点：不在起点就别硬走 —— 直接说清楚"你现在在哪、应该在哪"。
    # 否则会在第 1 步报"找不到锚点"，让人误以为是路径图错了（实际是先没到起点）。
    if start == 1:
        _xml0 = d.dump_hierarchy()
        _sig0 = npm_identity(_xml0, pkg)
        _src = pathmap.find_node(nodes, args.src)
        exp0 = pathmap.finish_expect(_src)
        ck0 = checkpoint.evaluate(checkpoint.parse_expect(exp0),
                                  activity=(d.app_current() or {}).get("activity"), sig=_sig0)
        if ck0["checked"] and not ck0["ok"]:
            _here = checkpoint.evaluate(
                checkpoint.parse_expect(""), activity="", sig=_sig0)
            emit(False, {"error": f"当前不在起点「{args.src}」",
                         "expect": exp0, "missed": ck0["missed"],
                         "hint": f"先导航到「{args.src}」再执行；"
                                 f"或用 --from 指定你实际所在的节点。"
                                 f"当前 activity={(d.app_current() or {}).get('activity')}"})
            return

    for i, step in enumerate(plan["steps"], 1):
        if i < start:
            results.append({"n": i, "from": step["from"], "to": step["to"],
                            "status": "skipped"})
            continue
        sel = pathmap.edge_selector(step)
        if not sel:
            _rec = {"n": i, "from": step["from"], "to": step["to"],
                    "status": "no_anchor", "reason": "这条边在路径图里没记锚点"}
            _e = log_auto("act", f"act steps[{i}] 中止",
                          f"第 {i} 步无法执行：这条边没记锚点",
                          {"batch_step": i, "reason": "no_anchor"}, None, 0,
                          datetime.now().isoformat(timespec="milliseconds"))
            _rec["event_id"] = _e
            results.append(_rec)
            break

        xml = d.dump_hierarchy()
        full = summarize_xml(xml, mode="full", fg_package=pkg)
        labeled = labeled_nodes(xml, pkg)
        spec = checkpoint.parse_expect(sel)
        hit = _find_anchor(full, labeled, spec)
        if not hit:
            # 锚点找不到也要落事件：否则整批没有任何事件 → ledger 判"无动作事件"。
            _png, _xml, _meta = evidence_paths(args.dir, args.name or f"steps{i:02d}")
            try:
                capture(d, _png, _xml, _meta, extra={"batch_step": i, "anchor_missing": sel})
            except Exception:  # noqa: BLE001
                _png = None
            _rec = {"n": i, "from": step["from"], "to": step["to"],
                    "status": "anchor_not_found", "selector": sel,
                    "reason": f"当前屏幕找不到锚点 {sel}"}
            _e = log_auto("act", f"act steps[{i}] {step['from']} → {step['to']}",
                          f"第 {i} 步失败：找不到锚点 {sel}",
                          {"batch_step": i, "anchor": sel, "status": "anchor_not_found"},
                          rel(_png) if _png else None, 0,
                          datetime.now().isoformat(timespec="milliseconds"))
            _rec["event_id"] = _e
            results.append(_rec)
            break

        d.click(hit["x"], hit["y"])
        time.sleep(args.settle / 1000)

        # 每步都要落证据 —— 批量执行**不能**降低取证标准。
        # 单步 act 的截图/dump 是 finding 绑定证据的前提；批量若只记一条汇总事件，
        # 事后就无法用 --event-id 引用（真实缺陷：会话 328 finding 被拒）。
        step_dir = None
        png = xml2_path = None
        try:
            _png, _xml, _meta = evidence_paths(args.dir, args.name or f"steps{i:02d}")
            capture(d, _png, _xml, _meta, extra={"batch_step": i,
                                                 "from": step["from"], "to": step["to"]})
            png, xml2_path, step_dir = _png, _xml, _meta
        except Exception as e:  # noqa: BLE001
            results.append({"n": i, "from": step["from"], "to": step["to"],
                            "status": "evidence_failed", "reason": str(e)})
            break

        xml2 = xml2_path.read_text(encoding="utf-8")
        act2 = None
        try:
            act2 = d.app_current().get("activity")
        except Exception:  # noqa: BLE001
            pass
        sig2 = npm_identity(xml2, pkg)
        tgt_node = pathmap.find_node(nodes, step["to"])
        exp = pathmap.finish_expect(tgt_node)
        ck = checkpoint.evaluate(checkpoint.parse_expect(exp), activity=act2, sig=sig2)
        rec = {"n": i, "from": step["from"], "to": step["to"],
               "anchor": step["anchor"], "selector": sel,
               "how": hit["how"], "clicked": [hit["x"], hit["y"]],
               "activity": act2, "expect": exp, "checkpoint": ck}
        # 每步一条事件，绑该步证据 —— 这样 finding 能按步引用
        eid = log_auto(
            "act", f"act steps[{i}] {step['from']} → {step['to']}",
            f"第 {i} 步 点 {hit['how']} ({hit['x']},{hit['y']}) → {act2}"
            + (" · ✅检查点命中" if ck["ok"] else
               " · ⚠️检查点未命中 " + "; ".join(ck["missed"])),
            {"action": {"type": "tap", "x": hit["x"], "y": hit["y"],
                        "via": sel, "why": args.why},
             "activity": act2, "sig": sig2, "checkpoint": ck, "batch_step": i},
            rel(png) if png else None, 0,
            datetime.now().isoformat(timespec="milliseconds"))
        rec["event_id"] = eid
        if ck["checked"] and not ck["ok"]:
            rec["status"] = "checkpoint_missed"
            results.append(rec)
            break
        rec["status"] = "ok"
        results.append(rec)

    done = [r for r in results if r.get("status") == "ok"]
    failed = next((r for r in results if r.get("status") not in ("ok", "skipped")), None)
    payload = {
        "route": plan["nodes"],
        "steps": results,
        "completed": len(done),
        "total": len(plan["steps"]),
        "ok": failed is None,
    }
    if failed:
        payload["failed_at"] = failed["n"]
        payload["hint"] = (f"第 {failed['n']} 步没过（{failed['status']}）："
                           f"{failed.get('reason') or failed.get('checkpoint', {}).get('missed')}。"
                           f"修好后可 `act.py steps --from ... --to ... --resume-from {failed['n']}` 接着跑。"
                           "先判断是临时弹窗（关掉继续）还是用例要的东西没了（停下来记录）。")
    emit(True, payload)


def main() -> None:
    p = argparse.ArgumentParser(description="act：执行动作并回传动作后画面")
    # --settle / --perm-action / --perm 已在 add_common_args 里定义
    add_common_args(p)
    sub = p.add_subparsers(dest="action", required=True)

    for name in ("tap", "longclick"):
        s = sub.add_parser(name)
        s.add_argument("--x", type=int, required=True)
        s.add_argument("--y", type=int, required=True)
        s.add_argument("--via", default=None, help="定位依据，如 rid=... 或 text=...")
        s.add_argument("--why", default=None, help="这一步的理由（入库展示在时间轴）")
        s.add_argument("--watch", default=None,
                       help="动作后监听瞬态弹窗并立即点击，多个候选用 | 分隔。"
                            "如 rid=com.android.permissioncontroller:id/permission_allow_button|text=允许")
        s.add_argument("--watch-timeout", type=int, default=8000, dest="watch_timeout",
                       help="watch 总窗口毫秒数，默认 8000（系统权限框约 6s 自行消失，必须一次处理完）")
        if name == "tap":
            s.add_argument("--repeat", type=int, default=1,
                           help="连点次数，默认 1。用于滚轮「点上方/下方一格=±1」这类"
                                "同一位置重复点按：N 次点按在**一个进程内**完成，"
                                "不要拆成 N 次工具调用（每次冷启动 3-6s，会撞命令超时，"
                                "超时会让动作执行了却没记账 → 会话被判 recording incomplete）。")
            s.add_argument("--gap", type=int, default=120,
                           help="--repeat 时每次点按之间的间隔毫秒，默认 120")

    s = sub.add_parser("input")
    s.add_argument("--x", type=int, required=True)
    s.add_argument("--y", type=int, required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--via", default=None)
    s.add_argument("--why", default=None)
    s.add_argument("--watch", default=None)
    s.add_argument("--watch-timeout", type=int, default=8000, dest="watch_timeout", help="watch 总窗口毫秒（系统权限框约6s自行消失）")
    s.add_argument("--no-click", action="store_true", dest="no_click",
                   help="不先点击聚焦（AI 已点过输入框时用，避免双击副作用）")
    s.add_argument("--append", action="store_true",
                   help="追加到已有内容（默认覆盖 clear=True）")

    s = sub.add_parser("swipe")
    s.add_argument("--from", dest="src", required=True, help="x1,y1")
    s.add_argument("--to", dest="dst", required=True, help="x2,y2")
    s.add_argument("--dur", type=int, default=300, help="毫秒，默认 300")
    s.add_argument("--via", default=None)
    s.add_argument("--why", default=None)
    s.add_argument("--watch", default=None)
    s.add_argument("--watch-timeout", type=int, default=8000, dest="watch_timeout", help="watch 总窗口毫秒（系统权限框约6s自行消失）")

    for name in ("back", "home"):
        s = sub.add_parser(name)
        s.add_argument("--why", default=None, help="这一步的理由（入库展示在时间轴）")

    s = sub.add_parser("key")
    s.add_argument("--code", type=int, required=True)
    s.add_argument("--why", default=None)

    # 批量走**路径图里已有的边**。第一次走某条路时用不着（边还没采到），
    # 第二次以及同事复用同一 SKILL 时，就不必再逐步 "看一眼 → 想一下 → 点一下"。
    s = sub.add_parser("steps", help="按路径图批量执行一串已知步骤（带检查点）")
    s.add_argument("--from", dest="src", required=True, help="起点节点名（路径图里的 ## 名）")
    s.add_argument("--to", dest="dst", required=True, help="目标节点名")
    s.add_argument("--max-hops", type=int, default=12, dest="max_hops")
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="只打印计划，不碰设备")
    s.add_argument("--resume-from", type=int, default=1, dest="resume_from",
                   help="从第 N 步开始（失败后修好接着跑，默认 1）")
    s.add_argument("--why", default=None)

    # 让 --serial/--dir/--name 在子命令前后都能写（两种位置解析后合并）
    add_common_args_all_subcommands(p)
    _reject_selector_misuse(p)
    args = p.parse_args()
    guard_terminated(f"act {args.action}")   # 会话已因崩溃终止 → 拒绝执行
    if args.action == "steps":
        run_steps(args)
        return
    via = getattr(args, "via", None)
    why = getattr(args, "why", None)

    # 顺带声明权限意图（在动作之前生效，这样本次动作触发的弹窗就能被接住）
    perm_inline = None
    if getattr(args, "perm_action", None):
        try:
            from db import current_session, get_db
            si = current_session()
            if si:
                get_db().set_perm_intent(si, args.perm_action, args.perm or "")
                perm_inline = {"action": args.perm_action, "permission": args.perm or ""}
        except Exception:  # noqa: BLE001
            pass

    try:
        d = connect(args.serial)
    except Exception as e:  # noqa: BLE001
        fail(f"设备连接失败: {e}")

    t0 = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="milliseconds")
    execution("may_have_executed")
    try:
        if args.action in ("tap", "longclick"):
            repeat = getattr(args, "repeat", 1) or 1
            gap = getattr(args, "gap", 120) or 0
            fn = d.long_click if args.action == "longclick" else d.click
            for i in range(repeat):
                if i:
                    time.sleep(gap / 1000)
                timed_call("action", fn, args.x, args.y)
            action = {"type": args.action, "x": args.x, "y": args.y}
            if repeat > 1:
                action["repeat"] = repeat
                action["gap_ms"] = gap
        elif args.action == "input":
            if not args.no_click:
                timed_call("action", d.click, args.x, args.y)
                timed_call("input_focus_wait", time.sleep, 0.3)
            timed_call("action", d.send_keys, args.text, clear=not args.append)
            action = {"type": "input", "x": args.x, "y": args.y, "text": args.text,
                      "mode": "append" if args.append else "overwrite"}
        elif args.action == "swipe":
            x1, y1 = xy(args.src)
            x2, y2 = xy(args.dst)
            timed_call("action", d.swipe, x1, y1, x2, y2, args.dur / 1000)
            action = {"type": "swipe", "from": [x1, y1], "to": [x2, y2], "dur_ms": args.dur}
        elif args.action == "back":
            timed_call("action", d.press, "back")
            action = {"type": "back"}
        elif args.action == "home":
            timed_call("action", d.press, "home")
            action = {"type": "home"}
        else:
            timed_call("action", d.shell, f"input keyevent {args.code}")
            action = {"type": "key", "code": args.code}
    except Exception as e:  # noqa: BLE001
        fail(f"动作执行失败: {e}")

    execution("executed")
    timed_call("settle_wait", time.sleep, args.settle / 1000)

    png, xml, meta = evidence_paths(args.dir, args.name)
    # ⚠️ 必须按 action 分支求值：字典字面量会**同时**求值所有 value，
    # 而 tap 没有 args.text / swipe 没有 args.x / back 没有 args.code
    # → AttributeError 让除 input 外的所有动作在采集阶段直接崩（真实事故）。
    if args.action in ("tap", "longclick"):
        detail = f"{'点击' if args.action == 'tap' else '长按'} ({args.x},{args.y})"
        _rep = getattr(args, "repeat", 1) or 1
        if _rep > 1:
            detail += f" ×{_rep}"
    elif args.action == "input":
        detail = (f"输入 {args.text!r}"
                  + ("（追加）" if getattr(args, "append", False) else ""))
    elif args.action == "swipe":
        detail = f"滑动 {args.src} → {args.dst}"
    elif args.action == "back":
        detail = "返回"
    elif args.action == "home":
        detail = "回到桌面"
    else:
        detail = f"keyevent {args.code}"
    if via:
        detail += f" via {via}"
        action["via"] = via
    if why:
        action["why"] = why

    # watch 在 capture 之前：瞬态弹窗的窗口期不能浪费在一次 dump 上。
    # ⚠️ 系统权限框约 6s 自动消失，必须先挂 watch 再让动作执行完，
    # 绝不能用"点一下 → observe → 再点"的节奏（会丢掉整个窗口）。
    watch_result = None
    watch_spec = getattr(args, "watch", None)
    if watch_spec:
        # 多阶段语法：阶段之间用 ";;" 分隔，阶段内候选用 "|" 分隔。
        # 例：--watch "text=知道了;;rid=...permission_allow_all_button|text=允许"
        #     （先点 App 提示框"知道了"，再点系统权限框"全部允许"）
        stages = [[s for s in stage.split("|") if s.strip()]
                  for stage in watch_spec.split(";;") if stage.strip()]
        watch_result = timed_call("watch", watch_and_click, d, stages,
                                  getattr(args, "watch_timeout", 8000))
        got = watch_result.get("clicked") or []
        if got:
            detail += " · watch命中 " + ", ".join(
                f"[阶段{c['stage'] + 1}]{c['spec']}" for c in got)
        if watch_result.get("missed_stage") is not None:
            detail += (f" · watch阶段{watch_result['missed_stage'] + 1}未出现"
                       f"（候选 {watch_result.get('missed_specs')}）")

    # 权限弹窗：按会话级意图自动响应（放在 capture 之前，别浪费 6s 窗口）。
    # 意图由 AI 事先声明；**未声明时默认 grant（同意）**——见 perm.DEFAULT_ACTION。
    perm_result = None
    try:
        from db import current_session, get_db
        si = current_session()
        intent = get_db().get_perm_intent(si) if si else None
        import perm as _perm
        # ★ 这里取的前台包 = **这个权限框是谁的权限**。
        #
        # 时机很关键：这时候**动作已经执行完了**，但 perm.handle 还没开始检测，
        # 所以拿到的仍是"点完按钮之后、权限框弹出之前"的前台 —— 也就是
        # **发起权限申请的那个 App**（= 被测 App）。
        # 如果放到 perm.handle 之后取，前台已经是权限框本身
        # （com.android.permissioncontroller），归属就错了，比不记更糟。
        #
        # 为什么需要它（真机踩过，169）：用例预期"点拍照导入应弹相机权限弹窗"，
        # 实际弹的是**日历自己的**「日历正在尝试打开摄像头」。两者都叫"相机权限"，
        # 但归属完全不同 —— 日历的是被测 App 的（要验），相机 App 的不是。
        # 不记归属，事后只看 prompts 那几个字根本分不出是谁弹的，
        # 就会出现"权限明明弹了、也允许了、相机也打开了，却判 FAIL"。
        #
        # 取失败不影响主流程（只是少了归属信息）。
        _owner = ""
        try:
            _owner = str((d.app_current() or {}).get("package") or "")
        except Exception:  # noqa: BLE001
            _owner = ""
        perm_result = timed_call("permission", _perm.handle,
                                 d, intent["action"] if intent else None,
                                 owner_package=_owner)
    except Exception as e:  # noqa: BLE001
        perm_result = {"error": str(e)}
    if perm_result and perm_result.get("detected"):
        clk = ", ".join(c["text"] for c in perm_result.get("clicks") or [])
        src = "（默认）" if perm_result.get("action_source") == "default" else ""
        detail += f" · 权限弹窗({perm_result.get('activity')})"
        if clk:
            detail += f" 已自动点击: {clk} {src}".rstrip()
        elif perm_result.get("unmatched"):
            detail += f" 未匹配可点按钮，可见: {perm_result.get('buttons')}"
        else:
            detail += " 未点击"

    try:
        info = capture(d, png, xml, meta,
                       extra={"action": action, **({"watch": watch_result} if watch_result else {})})
    except Exception as e:  # noqa: BLE001
        fail(f"动作已执行但采集失败: {e} | action={action}")

    # 崩溃自动检测（分级 + 20s 上下文取证，crash.log 落在当前步骤证据目录）
    failure = None
    crash = check_crash(d, info.get("package"), png.parent)
    alert = None
    if crash and crash.get("new"):
        from logcat import verdict as crash_verdict
        v = crash_verdict(crash)
        entry = crash.get("entry") or {}
        scope = crash.get("scope")
        info["crash_detected"] = True
        info["crash"] = {"scope": scope, "type": entry.get("type"),
                         "process": entry.get("process"), "time": entry.get("time"),
                         "log_path": crash.get("log_path")}
        detail += f" · ⚠️{v['message']}"
        if v["blocked"]:
            alert = v["message"]
            # 崩溃一次即终止：写会话终态（工具层硬拦截后续动作），
            # 并置 blocked 标记，AI 必须立即 finish 而不再继续
            info["blocked"] = True
            failure = {"blocked": True, "scope": scope,
                       "type": entry.get("type"), "process": entry.get("process"),
                       "reason": v["message"]}
            try:
                from db import current_session, get_db
                sid = current_session()
                if sid:
                    get_db().terminate_session(sid, v["message"])
            except Exception:  # noqa: BLE001
                pass


    # ⚠️ 事件必须在**补齐 perm/watch 等附加字段之后**再落库：
    # 这些字段是链路卡回放的关键（🎯 权限意图声明、🔐 权限框响应），
    # 若在组装前落库，卡里就只剩裸动作，回放者不知道当时声明了什么意图（真实缺陷）。
    if watch_result:
        info = {**info, "watch": watch_result}
    if perm_inline:
        info = {**info, "perm_intent_set": perm_inline}
    if perm_result and perm_result.get("detected"):
        info = {**info, "permission": perm_result}
    # 落库前先算一份**屏幕身份签名**（`kind=value` 集合），随事件一起存。
    # 用途：事后自动整理路径边时，判断"这一步落在路径图的哪个节点"。
    # 只存签名不存整个 nodes —— 签名几十个短串，nodes 可能有上百个对象。
    _sig = npm_identity(xml.read_text(encoding="utf-8"),
                        None if getattr(args, "no_filter", False) else info.get("package"))
    info = {**info, "sig": _sig}
    # 检查点：由工具判"这一步落在预期位置没有"。
    # 让 AI 不必再读 dump 自己认页面（那是会话时长的大头 —— 实测 58~64% 花在
    # 两次调用之间的判断上）。命中与否都返回，但**不阻断**：工具只报事实，
    # 由 AI 决定继续还是修正（测试里"实际发生了什么"比"帮它走通"重要）。
    _ck = checkpoint.evaluate(checkpoint.parse_expect(getattr(args, "expect", None)),
                              activity=info.get("activity"), sig=_sig)
    if _ck["checked"]:
        info = {**info, "checkpoint": _ck}
        detail += (" · ✅检查点命中" if _ck["ok"]
                   else " · ⚠️检查点未命中: " + "; ".join(_ck["missed"]))
    _transient = checkpoint.detect_transient(_sig)
    if _transient:
        info = {**info, "transient_popup": _transient}
    duration_ms = round((time.perf_counter() - t0) * 1000)
    info["duration_scope"] = "core operation through capture/signature/crash check; excludes connection, event write and response"
    log_auto("act", f"act {args.action}", detail, info, rel(png), duration_ms, started_at)

    nodes = summarize_xml(xml.read_text(encoding="utf-8"),
                          mode="full" if getattr(args, "full", False) else "nav",
                          fg_package=None if getattr(args, "no_filter", False)
                          else info.get("package"))
    payload = {
        "action": action,
        **info,
        "evidence": {"png": str(png), "xml": str(xml), "meta": str(meta)},
        # 「下一步能点什么」精简清单放最前 —— 直接从上一层的返回走到下一步，
        # **不需要再单独 observe 拿坐标**（那会多花 2s 工具 + 10~15s 决策等待）。
        "next": next_targets(nodes, labeled=labeled_nodes(
            xml.read_text(encoding="utf-8"),
            None if getattr(args, "no_filter", False) else info.get("package"))),
        "nodes": nodes,
        "nodes_count": len(nodes),
        "nodes_mode": "full" if getattr(args, "full", False) else "nav",
        "hint": ("下一步点 `next` 里的目标（c=坐标，t=文字，r=rid）；"
                 "需要按页面文字断言时看 nodes（--full 才是全量）。"
                 "toast 等浮层不在 nodes 里，用 read.py OCR"),
    }
    if perm_inline:
        payload["perm_intent_set"] = perm_inline
        detail = (f"[意图:{perm_inline['action']}"
                  + (f"/{perm_inline['permission']}" if perm_inline["permission"] else "")
                  + "] " + detail)
    if alert:
        payload["alert"] = alert
    if _ck["checked"]:
        payload["checkpoint"] = _ck
        if not _ck["ok"]:
            payload["checkpoint_hint"] = (
                "⚠️ 落点与预期不符：未命中 " + "; ".join(_ck["missed"]) + "。"
                "先判断是「多余的临时弹窗」（不影响主流程 → 关掉继续）还是"
                "「用例要的东西不存在了」（主流程被改 → 停止并如实记录）。")
    if _transient:
        payload["transient_popup"] = _transient
        payload["transient_hint"] = (
            "检测到已知临时弹窗，可关闭后继续（不影响主流程）。"
            "若它每次都出现，则应作为流程节点记入路径图，而非当弹窗处理。")
    if perm_result and perm_result.get("detected"):
        payload["permission"] = perm_result
        if not (perm_result.get("clicks") or []):
            payload["permission_hint"] = (
                "检测到系统权限弹窗但**没有自动点击**：弹窗里没有可匹配的按钮"
                f"（可见 {perm_result.get('buttons')}）。"
                "若是要测「拒绝」路径，用 perm-intent --action deny 后重触发；"
                "否则看 buttons 自己决定点哪个。")
    if failure:
        # 硬性终止信号：AI 必须立即 finish(BLOCKED)，不要再发下一个 act
        payload["terminate"] = True
        payload["terminate_reason"] = failure["reason"]
        payload["hint"] = ("⛔ 检测到被测/关联包崩溃 → 立即停止执行："
                           "session.py finding --status BLOCKED ... 然后 "
                           "session.py finish --status BLOCKED ...；不要继续下一步")
    emit(True, payload)


if __name__ == "__main__":
    main()
