"""Independent invocation ledger. Writes the start before launching a tool."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from runtime import ROOT, STORAGE_DIR, call_path

TOOLS = ("act", "observe", "read", "session", "state", "logcat", "vision")


def write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_events(storage: Path, call_id: str) -> list[dict]:
    path = storage / "sessions.db"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if "call_id" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}:
            return []
        return [dict(row) for row in conn.execute(
            "SELECT id, session_id, kind, data_json FROM events WHERE call_id=? ORDER BY id",
            (call_id,))]


def is_usage_error(record: dict) -> bool:
    """判断这次调用是否"在工具入口就被拒绝、确定没触达设备"。

    判据（必须同时满足，避免把真实执行失败误判成未执行）：
      1. 返回码为 2 —— argparse 用法错误的固定退出码
      2. 完全没有结构化输出 —— 工具没跑到 emit 那一步
      3. 没有采到任何 timing 分段 —— 执行路径根本没进入
    满足时该调用不构成"悬空动作"，可以修正参数后重试。
    """
    if record.get("returncode") != 2:
        return False
    if record.get("result") is not None:
        return False
    if record.get("timing"):
        return False
    return True


def reconcile(record: dict, events: list[dict]) -> dict:
    if record.get("status") == "started":
        return {"complete": False, "outcome": "interrupted",
                "issues": ["invocation has no end record"]}
    issues = []
    result = record.get("result")
    structured = isinstance(result, dict)
    unsuccessful = record.get("returncode") != 0 or (structured and result.get("ok") is False)
    refused = bool(structured and (result.get("rejected") or result.get("terminated")))
    outcome = "refused" if refused else ("failed" if unsuccessful else "succeeded")
    not_executed = bool(structured and result.get("execution_state") == "not_executed")
    if record.get("status") == "launch_failed":
        not_executed = True
    if record.get("audit_error"):
        issues.append(record["audit_error"])
    if structured and result.get("recording_failed"):
        issues.append("tool reported recording failure")
    if not structured and not isinstance(result, list):
        # 未结构化的返回分两类，性质完全不同：
        #   - 用法错误（argparse exit 2）：工具入口即拒绝，确定未触达设备 —— 不算记录缺陷
        #   - 其他：无法判断是否已执行，按"可能已执行"保守处理
        if is_usage_error(record):
            not_executed = True
            issues.append("tool rejected arguments (usage error); device untouched")
        elif (record.get("status") != "launch_failed"
                and record.get("structured_result_required", True)):
            issues.append("missing structured tool result")
    if record.get("expects_event") and not_executed and events:
        issues.append("not-executed result contradicts recorded operation events")
    if record.get("expects_event"):
        if structured:
            reported = result.get("event_ids", [])
            actual = [event["id"] for event in events]
            if reported != actual:
                issues.append("reported event IDs do not match database events")
            if result.get("call_id") != record["call_id"]:
                issues.append("result invocation ID mismatch")
        if not events and not (not_executed and unsuccessful):
            issues.append("no recorded observation/action event")
        elif events and not any(event["kind"] == record["tool"] for event in events):
            issues.append("primary tool event is missing")
    expected_session = record.get("session_id")
    if expected_session is not None and any(
            event["session_id"] != expected_session for event in events):
        issues.append("event session mismatch")
    # 未执行的调用不构成记录缺陷：它没有留下需要人工裁决的"悬空动作"。
    # 只保留说明性 issue 供审计，但不参与 complete 判定。
    blocking = [i for i in issues if not (not_executed and i.endswith("; device untouched"))]
    return {"complete": not blocking, "outcome": outcome, "issues": issues,
            "executed": not not_executed,
            "event_ids": [event["id"] for event in events]}


def expects_event(tool: str, arguments: list[str]) -> bool:
    if "--help" in arguments or "-h" in arguments:
        return False
    # `act steps --dry-run` 只算路线、**不碰设备也不落事件** —— 若仍按"应落事件"
    # 记账，ledger 会留下"started 但无事件"，把整个会话锁成不可重放（真实缺陷）。
    if tool == "act" and arguments and arguments[0] == "steps" and "--dry-run" in arguments:
        return False
    if tool in ("act", "observe", "read", "state"):
        return True
    return tool == "logcat" and any(arg in ("crash", "clear") for arg in arguments)


def launch(tool: str, arguments: list[str], storage: Path, session_id: int | None = None) -> int:
    call_id = uuid.uuid4().hex
    if session_id is None:
        from db import current_session
        session_id = current_session()
        if tool == "session" and arguments and arguments[0] == "start":
            session_id = None
        elif "--id" in arguments:
            session_id = int(arguments[arguments.index("--id") + 1])
    path = call_path(storage, session_id, call_id)
    needs_event = expects_event(tool, arguments)
    if needs_event and session_id is None:
        raise ValueError("Audited device calls require a session; use session start first")
    record = {
        "call_id": call_id, "session_id": session_id, "tool": tool,
        "started_at": datetime.now().isoformat(timespec="milliseconds"),
        "status": "started", "expects_event": needs_event,
        "structured_result_required": "--help" not in arguments and "-h" not in arguments,
    }
    write_record(path, record)
    env = {**os.environ, "ANDROID_TEST_STORAGE": str(storage),
           "ANDROID_TEST_CALL_ID": call_id, "PYTHONIOENCODING": "utf-8",
           "ANDROID_TEST_TIMING_FILE": str(storage / "timing" / f"{call_id}.json")}
    if session_id is not None:
        env["ANDROID_TEST_SESSION_ID"] = str(session_id)
    else:
        env.pop("ANDROID_TEST_SESSION_ID", None)
    started = time.perf_counter()
    try:
        process = subprocess.run(
            [sys.executable, str(ROOT / "tools" / f"{tool}.py"), *arguments],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as error:
        record.update(status="launch_failed", returncode=None, error=str(error),
                      ended_at=datetime.now().isoformat(timespec="milliseconds"),
                      wall_ms=round((time.perf_counter() - started) * 1000, 3))
        record["audit"] = reconcile(record, [])
        write_record(path, record)
        raise
    record.update(status="ended", returncode=process.returncode,
                  ended_at=datetime.now().isoformat(timespec="milliseconds"),
                  wall_ms=round((time.perf_counter() - started) * 1000, 3))
    sys.stdout.write(process.stdout)
    sys.stderr.write(process.stderr)
    sys.stdout.flush()
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        result = None
    record["result"] = result
    destination = path
    if tool == "session" and arguments and arguments[0] == "start" and isinstance(result, dict):
        record["session_id"] = result.get("session_id")
        destination = call_path(storage, record["session_id"], call_id)
    timing_path = Path(env["ANDROID_TEST_TIMING_FILE"])
    if timing_path.exists():
        try:
            record["timing"] = json.loads(timing_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            record["audit_error"] = f"Timing read failed: {error}"
    record["response_bytes"] = len(process.stdout.encode("utf-8"))
    for line in process.stderr.splitlines():
        try:
            diagnostic = json.loads(line)
        except ValueError:
            continue
        if isinstance(diagnostic, dict) and diagnostic.get("audit_failed"):
            record["audit_error"] = f"Child audit failure: {diagnostic.get('stage')}: {diagnostic.get('error')}"
    try:
        events = read_events(storage, call_id)
    except sqlite3.Error as error:
        events = []
        record["audit_error"] = f"Event reconciliation failed: {error}"
    record["audit"] = reconcile(record, events)
    try:
        write_record(path, record)
        if destination != path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            path.replace(destination)
            path = destination
        if needs_event and not record["audit"]["complete"] and session_id is not None:
            write_record(storage / "recording_errors" / str(session_id) / f"{call_id}.json",
                         {"call_id": call_id, "session_id": session_id,
                          "error": "Invocation evidence is incomplete",
                          "audit": record["audit"]})
    except OSError as error:
        print(json.dumps({"ok": False, "audit_failed": True, "call_id": call_id,
                          "session_id": session_id, "stage": "ledger_write",
                          "error": str(error), "ledger": str(path),
                          "hint": "Tool output was preserved; do not replay actions."},
                         ensure_ascii=False), file=sys.stderr)
        return process.returncode or 1
    # ⚠️ 账本路径默认**不打到 stderr**：调用方（PowerShell / CI）会把 stderr 的
    # 任何输出当成命令失败（NativeCommandError），导致 audit 通过却整体判失败。
    # 需要时用 ANDROID_TEST_VERBOSE=1 打开，仍走 stderr（stdout 归工具的结构化结果独占）。
    if os.environ.get("ANDROID_TEST_VERBOSE") == "1":
        print(f"Invocation ledger: {path}", file=sys.stderr)
    if not record["audit"]["complete"]:
        print(json.dumps({"ok": False, "audit_failed": True, "call_id": call_id,
                          "ledger": str(path), **record["audit"]}, ensure_ascii=False),
              file=sys.stderr)
    return process.returncode if process.returncode else (0 if record["audit"]["complete"] else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", type=int)
    parser.add_argument("tool", choices=TOOLS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    return launch(args.tool, args.arguments, STORAGE_DIR, args.session_id)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": str(error), "stage": "launcher"},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
