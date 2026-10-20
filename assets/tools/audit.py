"""Reconcile independent invocation records against persisted events."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import sqlite3
import sys

from run import read_events, reconcile
from runtime import STORAGE_DIR, invocation_paths


def report(session_id: int | None = None) -> dict:
    calls = []
    known_ids = set()
    for path in sorted(invocation_paths(STORAGE_DIR, session_id)):
        record = json.loads(path.read_text(encoding="utf-8"))
        if session_id is not None and record.get("session_id") != session_id:
            continue
        known_ids.add(record["call_id"])
        events = read_events(STORAGE_DIR, record["call_id"])
        calls.append({
            "call_id": record["call_id"], "tool": record["tool"],
            "status": record["status"], "wall_ms": record.get("wall_ms"),
            "started_at": record.get("started_at"), "ended_at": record.get("ended_at"),
            "timing": record.get("timing"),
            "response_bytes": record.get("response_bytes"),
            **reconcile(record, events),
        })
    calls.sort(key=lambda call: (call["started_at"] or "", call["call_id"]))
    previous_end = None
    for call in calls:
        call["tool_interval_ms"] = None
        if previous_end and call["started_at"]:
            call["tool_interval_ms"] = round((
                datetime.fromisoformat(call["started_at"]) -
                datetime.fromisoformat(previous_end)).total_seconds() * 1000, 3)
        previous_end = call["ended_at"]
    untracked = []
    path = STORAGE_DIR / "sessions.db"
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "call_id" in columns:
                query = "SELECT id, call_id FROM events"
                params = ()
                if session_id is not None:
                    query += " WHERE session_id=?"
                    params = (session_id,)
                untracked = [row[0] for row in conn.execute(query, params)
                             if row[1] not in known_ids]
            else:
                query = "SELECT id FROM events"
                params = ()
                if session_id is not None:
                    query += " WHERE session_id=?"
                    params = (session_id,)
                untracked = [row[0] for row in conn.execute(query, params)]
    return {
        "ok": bool(calls) and all(call["complete"] for call in calls) and not untracked,
        "calls": calls, "untracked_event_ids": untracked,
        "failed_attempts": [call for call in calls if call["outcome"] != "succeeded"],
        "session_wall_ms": session_wall_ms(session_id),
        "scope": ("wall_ms covers child process only; tool_interval_ms includes host, user and launcher "
                  "overhead, not pure model inference. Negative intervals indicate overlap/clock changes. "
                  "Host transcript is needed to detect calls never reaching launcher."),
    }


def session_wall_ms(session_id: int | None) -> float | None:
    path = STORAGE_DIR / "sessions.db"
    if session_id is None or not path.exists():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT started_at, finished_at FROM sessions WHERE id=?",
                           (session_id,)).fetchone()
    if not row or not row[1]:
        return None
    return round((datetime.fromisoformat(row[1]) -
                  datetime.fromisoformat(row[0])).total_seconds() * 1000, 3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", type=int)
    args = parser.parse_args()
    result = report(args.session_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": str(error), "stage": "audit"},
                         ensure_ascii=False))
        raise SystemExit(1)
