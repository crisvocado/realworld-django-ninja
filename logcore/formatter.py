"""Payload formatter adhering to Logcore wire shape and golden entry contracts."""

import hashlib
import json
import logging
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from logcore.config import LogcoreConfig, get_config
from logcore.trace import get_current_trace_context, trace_context_to_dict

_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


def _is_in_app(filename: str, base_dir: Path | None = None) -> bool:
    if not filename:
        return False
    path_str = filename.replace("\\", "/")
    if "/site-packages/" in path_str or "/.venv/" in path_str or "/venv/" in path_str:
        return False
    if "/lib/python" in path_str or "<" in path_str:
        return False
    target_base = base_dir or _WORKSPACE_ROOT
    target_str = str(target_base).replace("\\", "/")
    if path_str.startswith(target_base.as_posix()) or path_str.startswith(target_str):
        return True
    return not path_str.startswith(("/usr/", "/opt/", "/lib/"))


def _parse_error(exc_info: Any, base_dir: Path | None = None) -> dict[str, Any] | None:
    if not exc_info or not isinstance(exc_info, tuple) or len(exc_info) < 2:
        return None
    exc = exc_info[1]
    if not isinstance(exc, BaseException):
        return None

    # Walk exception chain from raised exception down to root cause
    chain: list[BaseException] = []
    curr: BaseException | None = exc
    visited: set[int] = set()
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        chain.append(curr)
        if curr.__cause__ is not None:
            curr = curr.__cause__
        elif not getattr(curr, "__suppress_context__", False) and curr.__context__ is not None:
            curr = curr.__context__
        else:
            break

    # Root exception first, so innermost frame of the root cause is index 0
    root_first_chain = list(reversed(chain))

    frames: list[dict[str, Any]] = []
    for e in root_first_chain:
        tb = e.__traceback__
        if tb is None:
            continue
        tb_frames = traceback.extract_tb(tb)
        # Invert frames within each traceback so the frame that threw is first
        for frame in reversed(tb_frames):
            in_app = _is_in_app(frame.filename, base_dir)
            frames.append(
                {
                    "function": frame.name,
                    "file": frame.filename,
                    "line": frame.lineno or 0,
                    "column": getattr(frame, "colno", 0) or 0,
                    "inApp": in_app,
                }
            )

    # Clamp to at most 50 frames
    frames = frames[:50]

    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "stack": frames,
    }


def _clamp_labels(labels: dict[str, Any]) -> dict[str, str]:
    clamped: dict[str, str] = {}
    for i, (k, v) in enumerate(labels.items()):
        if i >= 32:
            break
        k_str = str(k)[:1024]
        v_str = str(v)[:1024]
        clamped[k_str] = v_str
    return clamped


def _canonical_json(obj: Any) -> str:
    try:
        return json.dumps(obj or {}, sort_keys=True, separators=(",", ":"))
    except Exception:
        return "{}"


def format_record(
    record: logging.LogRecord,
    config: LogcoreConfig | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    cfg = config or get_config()

    # Timestamp in UTC ISO 8601 format with millisecond precision and Z suffix
    created_ts = getattr(record, "created", None)
    if created_ts:
        dt = datetime.fromtimestamp(created_ts, tz=UTC)
    else:
        dt = datetime.now(UTC)
    timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    severity = record.levelname.upper() if getattr(record, "levelname", None) else "ERROR"
    if severity == "FATAL":
        severity = "CRITICAL"

    raw_message = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)
    message = raw_message[:65536]

    service = cfg.service
    env = cfg.env

    # Labels and Context
    labels: dict[str, Any] = {"logger": record.name}
    extra_labels = getattr(record, "labels", None)
    if isinstance(extra_labels, dict):
        for k, v in extra_labels.items():
            labels[str(k)] = v
    clamped_labels = _clamp_labels(labels)

    context: dict[str, Any] = {}
    extra_context = getattr(record, "context", None)
    if isinstance(extra_context, dict):
        for k, v in extra_context.items():
            context[str(k)] = v

    # Insert ID: deterministic sha256 hash truncated to 32 hex chars
    raw_insert = f"{timestamp}|{service}|{severity}|{message}|{_canonical_json(context)}"
    insert_id = hashlib.sha256(raw_insert.encode("utf-8")).hexdigest()[:32]

    # Trace
    trace_dict: dict[str, Any] | None = None
    extra_trace = getattr(record, "trace", None)
    if isinstance(extra_trace, dict):
        trace_dict = {str(k): v for k, v in extra_trace.items()}
    else:
        trace_ctx = get_current_trace_context()
        trace_dict = trace_context_to_dict(trace_ctx)

    # Error
    error_dict = _parse_error(getattr(record, "exc_info", None), base_dir=base_dir)

    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "severity": severity,
        "message": message,
        "service": service,
        "env": env,
        "insert_id": insert_id,
    }

    if trace_dict is not None:
        entry["trace"] = trace_dict

    if error_dict is not None:
        entry["error"] = error_dict

    if clamped_labels:
        entry["labels"] = clamped_labels

    if context:
        entry["context"] = context

    return entry
