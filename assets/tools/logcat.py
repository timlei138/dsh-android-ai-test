"""logcat：崩溃/ANR 取证 + 分级检测。

分级规则（崩溃对用例结论的影响）：
  target   被测包崩溃/ANR            → 用例应判 BLOCKED（AI 记 finding + finish）
  related  关联包（会话声明的 + 权限框）崩溃/ANR → 同 BLOCKED
  other    无关包崩溃                → 仅记录，测试报告中说明即可

每条崩溃自动抓"崩溃时刻前 20s"的 main+system+crash 日志落盘为证据。
会话 start 时执行 logcat -G 5M 扩缓冲防冲 + 清 crash 缓冲区归因。

用法：
  python logcat.py crash [--package com.app] [--tail 500] [--raw]
  python logcat.py clear
  python logcat.py setup               # logcat -G 5M（session start 自动做）
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

from common import (ROOT, add_common_args_all_subcommands, connect, emit, fail,
                    log_auto, rel)
from runtime import STORAGE_DIR, timed_call, execution

# 注意：crash buffer 每行有 logcat 标准前缀，标记在行中间，不能用 ^ 锚定
# ⚠️ native crash（*** FATAL / tombstone）结构不同：无标准前缀与 Process: 行，
# 大概率解析为 process=None → 归 other，由 AI 人工判读——未覆盖，勿当已支持。
_MARK = re.compile(r"FATAL EXCEPTION|ANR in|\*\*\* FATAL")
_PROC = re.compile(r"Process:\s*([^\s,;]+)")
_TS = re.compile(r"(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)")
# logcat 标准前缀：时间之后第一个数字段是 PID
_PID = re.compile(r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+(\d+)\s", re.M)
# 默认关联包：权限弹窗宿主（权限测试必经），会话可用 --related 追加
DEFAULT_RELATED = ("com.android.permissioncontroller",
                   "com.google.android.permissioncontroller")


def parse_entries(raw: str) -> list[dict]:
    """按 FATAL/ANR 标记切块，提取进程/PID/时间。

    ⚠️ 标记在 logcat 行中间（前缀是 时间 PID TID 级别 Tag），切块必须先回溯到
    行首再取——否则时间戳/PID 被切在块外，capture_context 拿不到时间，
    "崩溃前 20s 取证"就是空承诺（本注释为修过的真实事故留）。
    """
    if not raw:
        return []
    marks = [m.start() for m in _MARK.finditer(raw)]
    entries = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(raw)
        line_start = raw.rfind("\n", 0, start) + 1   # 回溯到标记所在行行首
        block = raw[line_start:end]
        first = block.splitlines()[0] if block.splitlines() else ""
        ts = _TS.search(first)
        proc = _PROC.search(block)
        process = proc.group(1) if proc else None
        if not process:
            # ANR 块没有 Process: 行：进程名在 ActivityManager 的 "ANR in <pkg>" 里。
            # 缺了这步，被测 App 的 ANR 会被分级成 other 而被放过（真实事故：复审抓到）。
            anr = re.search(r"ANR in\s+([\w.$]+)", block)
            if anr:
                process = anr.group(1)
        # PID 优先取 Process 行里崩溃进程自己的（指纹去重要区分崩溃风暴），
        # 没有再用 logcat 前缀里的写入者 PID 兜底
        pid = None
        pidm = re.search(r"PID:\s*(\d+)", block)
        if pidm:
            pid = pidm.group(1)
        else:
            pre = _PID.search(first)
            pid = pre.group(1) if pre else None
        # head 去掉 logcat 前缀，保留 Tag 之后的异常描述
        head = first.split("] ")[-1]
        if ": " in head:
            head = head.split(": ", 1)[1]
        entries.append({"type": "ANR" if "ANR" in head else "CRASH",
                        "process": process,
                        "pid": pid,
                        "time": ts.group(1) if ts else None,
                        "head": head[:80]})
    return entries


def _pkg_match(process: str | None, pkg: str | None) -> bool:
    """包名与进程名全等匹配（仅剥离 :subprocess 后缀）。

    ⚠️ 不能用子串，也不能用点分前缀：`com.zui.calendar.overlay.xxx` 是独立包，
    它的崩溃既不能算 `com.zui.calendar` 的，反过来也不行（_system.md 同族事故）。
    同 App 的多进程（如 com.zui.calendar:push）按 `:` 后缀剥离后全等命中。
    """
    if not process or not pkg:
        return False
    return process.strip().split(":")[0] == pkg


def classify(process: str | None, target: str | None, related: list[str]) -> str:
    if _pkg_match(process, target):
        return "target"
    if any(_pkg_match(process, r) for r in related):
        return "related"
    return "other"


def capture_context(d, device_ts: str | None, max_lines: int = 2500) -> str | None:
    """抓"崩溃时刻前 20s"的上下文日志，**crash 段优先**。

    ⚠️ 两个真实事故决定了这个实现（勿回退）：

    1. 合并取 `-b main,system,crash` 时，main 的海量普通日志排在 crash 段之前：
       实测窗口 10201 行里 FATAL EXCEPTION 在**第 3529 行**。无论 `lines[:N]`
       还是 `lines[-N:]`，只要 N=2500 就必然把崩溃栈切掉 →
       crash.log"有文件但没现场"。
    2. 所以先单独取 crash 缓冲（实测仅 ~24 行，且**必含**完整崩溃栈），
       再用剩余配额去取 main+system 的 20s 前情做上下文。
       这样无论普通日志多吵，崩溃现场都不会丢。
    """
    if not device_ts:
        return None
    try:
        t = datetime.strptime(device_ts, "%m-%d %H:%M:%S.%f")
    except ValueError:
        return None
    since = (t - timedelta(seconds=20)).strftime("%m-%d %H:%M:%S.%f")[:-3]

    parts: list[str] = []
    # ① 崩溃本体优先（小块、必含栈）
    try:
        crash_txt = (d.shell(f'logcat -d -b crash -t "{since}"').output or "")
    except Exception:  # noqa: BLE001
        crash_txt = ""
    if crash_txt.strip():
        parts.append("# ===== crash buffer（崩溃本体） =====")
        parts.append(crash_txt.strip())

    # ② 用剩余配额补 main+system 前情（保尾部：紧邻崩溃的那段上下文最有用）
    used = sum(len(p.splitlines()) for p in parts)
    budget = max(0, max_lines - used)
    if budget:
        try:
            ctx = (d.shell(f'logcat -d -b main,system -t "{since}"').output or "")
        except Exception:  # noqa: BLE001
            ctx = ""
        cl = ctx.splitlines()
        if cl:
            parts.append("")
            parts.append("# ===== main+system（崩溃前 20s 上下文） =====")
            if len(cl) > budget:
                parts.append(f"... [已省略较早的 {len(cl) - budget} 行，仅保留崩溃前后段] ...")
                cl = cl[-budget:]
            parts.extend(cl)

    return "\n".join(parts) if parts else None


def detect(d, target: str | None, related: list[str],
           evidence_dir: Path | None = None) -> dict | None:
    """act 每步调用：查 crash buffer → 分级 → 抓 20s 上下文落盘。

    去重按崩溃指纹（进程+时间+头部哈希），不是行数——行数会因缓冲区轮转
    和 -t 截断失真，导致漏报。返回 None=无崩溃。
    """
    try:
        # 全量 dump（crash 缓冲区只存崩溃/ANR，体量小；-t 截断会让行数去重失真）
        raw = (d.shell("logcat -b crash -d").output or "")
    except Exception:  # noqa: BLE001
        return None
    related = list(related) + [r for r in DEFAULT_RELATED if r not in related]
    entries = parse_entries(raw)
    if not entries:
        return None

    # 指纹去重：进程+PID+时间+头部——同 App 同秒的崩溃风暴靠 PID 区分，不互吞。
    # 去重文件按会话隔离（.crash_seen_<sid>），避免上一会话的旧指纹吞掉新会话的崩溃。
    import hashlib
    try:
        from db import current_session
        sid = current_session() or "loose"
    except Exception:  # noqa: BLE001
        sid = "loose"
    mark = STORAGE_DIR / f".crash_seen_{sid}"
    fps = [hashlib.sha1(
        f"{e.get('process')}|{e.get('pid')}|{e.get('time')}|{e.get('head')}".encode()
    ).hexdigest()[:12] for e in entries]
    seen: set[str] = set()
    try:
        seen = {l.strip() for l in mark.read_text(encoding="utf-8").splitlines() if l.strip()}
    except (OSError, ValueError):
        pass
    fresh = [(fp, e) for fp, e in zip(fps, entries) if fp not in seen]
    if not fresh:
        return {"known": True}
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text("\n".join(sorted(seen | set(fps))), encoding="utf-8")
    except OSError:
        pass

    scoped = [(classify(e.get("process"), target, related), e) for _, e in fresh]
    # ⚠️ 定级必须取**全部新崩溃中最高危的那条**，不能只看最新一条：
    # 被测包先崩、随后一个无关包崩时，只看最新会判成 other 而不阻断，
    # 而那条 target 崩溃已被写进指纹文件、**永远不再上报**（真实缺陷）。
    # 危害等级：target > related > other
    # ⚠️ tie-break 必须带下标：max() 平局时返回**首次出现**（最旧）那条，
    # 同级多包崩溃时会指错进程（报告里的 process/time 就是它）。
    # (rank, i) 让同级取**最新**——离现在最近、最该被报出来的那条。
    _RANK = {"target": 3, "related": 2, "other": 1}
    idx = max(range(len(scoped)),
              key=lambda i: (_RANK.get(scoped[i][0], 0), i))
    worst_scope, worst = scoped[idx]
    latest = scoped[-1][1]

    # 取证窗口锚点：
    #   最高危那条**晚于**最新条时不可能（它就是候选之一），故只需考虑
    #   "最高危比最新更早"的情况 —— 取更早的时刻，保证最严重的那次也在窗口内。
    #   同危时用最新（上下文离现在最近、更有价值）。
    anchor = latest.get("time")
    if (worst.get("time") and anchor and worst["time"] < anchor
            and _RANK.get(worst_scope, 0) > _RANK.get(scoped[-1][0], 0)):
        anchor = worst["time"]
    ctx = timed_call("crash_evidence", capture_context, d, anchor)
    log_path = None
    if ctx and evidence_dir is not None:
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            timed_call("crash_evidence_save", (evidence_dir / "crash.log").write_text,
                       ctx, encoding="utf-8")
            log_path = rel(evidence_dir / "crash.log")
        except OSError:
            pass
    return {"new": True, "scope": worst_scope, "entry": worst,
            # 兼容/可读性：最新那条也给出（entry 是"最高危"，latest 是"最新"）
            "latest": latest,
            "in_scope_count": sum(1 for s, _ in scoped if s in ("target", "related")),
            # 同批里所有被测/关联包崩溃（AI 报告需要看全，不只看最高危那条）
            "in_scope": [{"scope": s, "process": e.get("process"),
                          "type": e.get("type"), "time": e.get("time")}
                         for s, e in scoped if s in ("target", "related")],
            "total_count": len(entries), "log_path": log_path}


def verdict(crash: dict | None) -> dict:
    """把崩溃检测结果翻译成**处置指令**（act/observe 共用，避免两处逻辑漂移）。

    规则（用户明确要求）：**被测/关联包崩溃一次即 BLOCKED，不再等等看**。
    偶发崩溃与必现崩溃在"已经崩过一次"这件事上没有区别——继续跑只会让
    后续步骤建立在不可信的环境上，所以立即停、立即 BLOCKED。

    返回 {"blocked": bool, "scope":..., "message":..., "kind":...}
      - blocked=True  → 必须记 BLOCKED finding 并立即 finish，**停止后续步骤**
      - blocked=False → 仅记录（无关包），不阻断
    """
    if not crash or not crash.get("new"):
        return {"blocked": False, "kind": "none", "message": ""}
    entry = crash.get("entry") or {}
    scope = crash.get("scope")
    what = entry.get("type") or "崩溃"
    proc = entry.get("process") or "?"
    if scope in ("target", "related"):
        who = "被测包" if scope == "target" else "关联包"
        return {
            "blocked": True, "scope": scope, "kind": "stop",
            "message": (f"{who} {proc} 发生{what}（已取证 crash.log）→ "
                        f"**立即停止后续步骤**：记 BLOCKED finding（写明异常类型与栈顶）"
                        f"并以 BLOCKED finish。崩溃一次即判 BLOCKED——"
                        f"偶发崩溃同样说明环境不可信，不要继续往下跑。"),
        }
    return {
        "blocked": False, "scope": scope, "kind": "note",
        "message": f"无关包 {proc} 发生{what}（不影响本用例判定，报告中说明即可）",
    }


def main() -> None:
    p = argparse.ArgumentParser(description="logcat：崩溃/ANR 取证")
    p.add_argument("--serial", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("crash", help="dump crash 缓冲区（会话开始后即为本会话内容）")
    s.add_argument("--package", default=None, help="只统计该包的崩溃块")
    s.add_argument("--tail", type=int, default=500, help="最多读取行数，默认 500")
    s.add_argument("--raw", action="store_true", help="附完整原始文本（截取末 6000 字符）")

    sub.add_parser("clear", help="清空 crash 缓冲区（会话 start 已自动做，手动重置用）")

    sub.add_parser("setup", help="logcat -G 5M 扩大缓冲区防冲（session start 自动做）")

    # --serial 写在子命令前后都能解析（与其他工具一致）
    add_common_args_all_subcommands(p)
    args = p.parse_args()
    execution("not_executed")
    if args.cmd != "crash":
        from common import guard_terminated
        guard_terminated(f"logcat {args.cmd}")
    try:
        d = connect(args.serial)
    except Exception as e:  # noqa: BLE001
        fail(f"设备连接失败: {e}")

    execution("may_have_executed")
    try:
        if args.cmd == "crash":
            raw = (d.shell(f"logcat -b crash -d -t {args.tail}").output or "")
            entries = parse_entries(raw)
            if args.package:
                # 与 classify 同规则：按段精确匹配，不做子串（overlay 同族事故）
                entries = [e for e in entries if _pkg_match(e["process"], args.package)]
            ctx = timed_call("crash_evidence", capture_context, d, entries[-1]["time"]) if entries else None
            log_path = None
            if ctx:
                log_dir = STORAGE_DIR / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                fp = log_dir / f"crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                fp.write_text(ctx, encoding="utf-8")
                log_path = rel(fp)
            detail = f"崩溃/ANR {len(entries)} 条" + (f"（{args.package}）" if args.package else "")
            execution("executed")
            log_auto("logcat", "logcat crash", detail,
                     {"count": len(entries), "entries": entries[:10], "log_path": log_path},
                     log_path)
            emit(True, {"count": len(entries), "entries": entries,
                        "log_path": log_path,
                        "raw": raw[-6000:] if args.raw else None,
                        "hint": "count>0 且 process 为被测/关联包 = 本次会话内崩溃；"
                                "前 20s 上下文日志已存 log_path；"
                                "判定'包名回桌面'类可疑信号必须以此为准，不能只凭画面猜"})
        elif args.cmd == "setup":
            out = (d.shell("logcat -G 5M").output or "").strip()
            execution("executed")
            emit(True, {"buffer": "5M", "output": out,
                        "hint": "主/系统/crash 缓冲区已扩到 5M，防止长会话日志被冲掉"})
        else:
            d.shell("logcat -b crash -c")
            execution("executed")
            log_auto("logcat", "logcat clear", "清空 crash 缓冲区", {})
            emit(True, {"cleared": True})
    except Exception as e:  # noqa: BLE001
        fail(f"logcat {args.cmd} 失败: {e}")


if __name__ == "__main__":
    main()
