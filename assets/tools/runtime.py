"""Measured stages and invocation identity; no device or model policy."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import time
import uuid
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

ROOT = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.environ.get("ANDROID_TEST_STORAGE", str(ROOT / "storage"))).resolve()
CALL_ID = os.environ.get("ANDROID_TEST_CALL_ID") or uuid.uuid4().hex
PROCESS_STARTED = time.perf_counter()
STAGES: dict[str, float] = {}
EVENT_IDS: list[int] = []
EXECUTION_STATE = "unknown"
P = ParamSpec("P")
T = TypeVar("T")


@contextmanager
def stage(name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        STAGES[name] = STAGES.get(name, 0.0) + (time.perf_counter() - started) * 1000


def timing_snapshot() -> dict:
    return {
        "call_id": CALL_ID,
        "measured_at": datetime.now().isoformat(timespec="milliseconds"),
        "process_observed_ms": round((time.perf_counter() - PROCESS_STARTED) * 1000, 3),
        "stages_ms": {name: round(value, 3) for name, value in STAGES.items()},
        "scope": "since runtime import; startup and earlier imports require launcher timing",
    }


def timed(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            with stage(name):
                return function(*args, **kwargs)
        return wrapper
    return decorate


def timed_call(name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    with stage(name):
        return function(*args, **kwargs)


def execution(state: str) -> None:
    global EXECUTION_STATE
    EXECUTION_STATE = state


def call_path(storage: Path, session_id: int | None, call_id: str) -> Path:
    return storage / "calls" / (str(session_id) if session_id is not None else "unbound") / f"{call_id}.json"


def invocation_paths(storage: Path, session_id: int | None = None):
    root = storage / "calls"
    root.mkdir(parents=True, exist_ok=True)
    migrated = root / ".partitioned"
    if not migrated.exists():
        for path in root.glob("*.json"):
            try:
                call = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(call, dict):
                    raise ValueError(f"Invalid invocation record: {path}")
                destination = call_path(storage, call.get("session_id"), path.stem)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError(f"Duplicate invocation record: {path}")
                path.rename(destination)
            except FileNotFoundError:
                if path.exists():
                    raise
        migrated.touch()
    if session_id is not None:
        return (root / str(session_id)).glob("*.json")
    return root.glob("*/*.json")


def recording_issues(storage: Path, session_id: int) -> list[str]:
    """本会话是否留下了需要人工裁决的"悬空动作"。

    ⚠️ 只锁定"可能已执行"的调用。用法错误（参数不对、工具入口即拒绝）
    确定没碰设备，不构成悬空动作 —— 否则一次手误就要作废整个会话。
    区分见 run.py:is_usage_error 与 audit 记录里的 executed 字段。
    """
    issues = []
    errors = storage / "recording_errors" / str(session_id)
    if errors.exists() and any(errors.iterdir()):
        issues.append("recording failure marker exists")
    for path in invocation_paths(storage, session_id):
        call = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(call, dict):
            raise ValueError(f"Invalid invocation record: {path}")
        audit = call.get("audit", {})
        if not isinstance(audit, dict):
            raise ValueError(f"Invalid invocation audit: {path}")
        if call.get("session_id") != session_id or not call.get("expects_event"):
            continue
        if call.get("call_id") == CALL_ID:
            continue
        # 明确未执行的调用放行（其动作没有发生，无需裁决）
        if audit.get("executed") is False:
            continue
        if call.get("status") == "started" or not audit.get("complete", False):
            issues.append(f"incomplete invocation: {call.get('call_id')}")
    return issues
