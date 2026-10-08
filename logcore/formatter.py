from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from logcore.context import current_trace

VALID_ENVS = {"prod", "staging", "dev", "test", "local"}
ENV_ALIASES = {
    "production": "prod",
    "development": "dev",
    "testing": "test",
    "stage": "staging",
}


def is_in_app(filepath: str | None) -> bool:
    if not filepath:
        return True
    norm = filepath.replace("\\", "/")
    if "site-packages" in norm or "dist-packages" in norm or "/.venv/" in norm or "/venv/" in norm or "/env/" in norm:
        return False
    if norm.startswith(("/usr/lib", "/usr/local/lib", "<frozen", "<string>", "<stdin>")):
        return False
    if sys.base_prefix and norm.startswith(sys.base_prefix.replace("\\", "/")):
        return False
    return True


def parse_stack(exc_value: BaseException | None, exc_tb: Any) -> list[dict[str, Any]]:
    if exc_value is None and exc_tb is None:
        return []

    # Follow exception chain: __cause__ or (__context__ if not __suppress_context__)
    chain: list[BaseException] = []
    curr: BaseException | None = exc_value
    seen: set[int] = set()
    while curr is not None and id(curr) not in seen:
        seen.add(id(curr))
        chain.append(curr)
        if curr.__cause__ is not None:
            curr = curr.__cause__
        elif not getattr(curr, "__suppress_context__", False) and curr.__context__ is not None:
            curr = curr.__context__
        else:
            break

    # Root exception is chain[-1], top exception is chain[0].
    # Innermost first: the root throw site at index 0.
    all_frames: list[dict[str, Any]] = []
    for exc in reversed(chain):
        tb = getattr(exc, "__traceback__", None) or (exc_tb if exc is exc_value else None)
        if tb is None:
            continue
        tb_frames = list(traceback.extract_tb(tb))
        for frame in reversed(tb_frames):
            col = getattr(frame, "colno", None)
            frame_dict: dict[str, Any] = {
                "function": (frame.name or "<unknown>")[:256],
                "file": frame.filename[:1024] if frame.filename else None,
                "line": frame.lineno,
                "inApp": is_in_app(frame.filename),
            }
            if col is not None:
                frame_dict["column"] = col
            all_frames.append(frame_dict)
            if len(all_frames) >= 50:
                break
        if len(all_frames) >= 50:
            break

    return all_frames[:50]


def format_severity(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "CRITICAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    if levelno >= logging.INFO:
        return "INFO"
    return "DEBUG"


class LogcoreFormatter:
    def __init__(self, service_name: str | None = None, env_name: str | None = None) -> None:
        raw_service = service_name or os.getenv("LOGCORE_SERVICE", "realworld-django-ninja")
        self.service = raw_service.lower().strip()[:63]
        raw_env = env_name or os.getenv("LOGCORE_ENV", "dev")
        raw_env_clean = raw_env.lower().strip()
        self.env = ENV_ALIASES.get(raw_env_clean, raw_env_clean)
        if self.env not in VALID_ENVS:
            self.env = "dev"

    def format_record(self, record: logging.LogRecord) -> dict[str, Any]:
        dt = datetime.fromtimestamp(record.created, tz=UTC)
        timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        severity = format_severity(record.levelno)
        raw_msg = record.getMessage() or record.msg or "Error"
        message = str(raw_msg)[:65536]
        if not message:
            message = "Error"

        context: dict[str, Any] = {}
        raw_ctx = getattr(record, "context", None)
        if isinstance(raw_ctx, dict):
            context = raw_ctx

        # Canonical context for deterministic insert_id
        canonical_ctx = json.dumps(context, sort_keys=True, separators=(",", ":"))
        insert_id_seed = f"{timestamp}|{self.service}|{severity}|{message}|{canonical_ctx}"
        insert_id = hashlib.sha256(insert_id_seed.encode("utf-8")).hexdigest()[:32]

        entry: dict[str, Any] = {
            "timestamp": timestamp,
            "severity": severity,
            "message": message,
            "service": self.service,
            "env": self.env,
            "insert_id": insert_id,
        }

        # Trace context
        trace_ctx = current_trace.get()
        if trace_ctx and isinstance(trace_ctx, dict) and trace_ctx.get("traceId"):
            trace_obj: dict[str, Any] = {
                "traceId": str(trace_ctx["traceId"]),
            }
            if trace_ctx.get("spanId") is not None:
                trace_obj["spanId"] = str(trace_ctx["spanId"])
            if trace_ctx.get("sampled") is not None:
                trace_obj["sampled"] = bool(trace_ctx["sampled"])
            entry["trace"] = trace_obj

        # Error / exception info
        exc_info = record.exc_info
        if isinstance(exc_info, tuple):
            try:
                exc_type, exc_value, exc_tb = exc_info
                if exc_type is not None or exc_value is not None:
                    type_name = exc_type.__name__ if exc_type else exc_value.__class__.__name__
                    msg_str = str(exc_value) if exc_value is not None else ""
                    frames = parse_stack(exc_value if isinstance(exc_value, BaseException) else None, exc_tb)
                    entry["error"] = {
                        "type": type_name[:256],
                        "message": msg_str[:65536],
                        "stack": frames,
                    }
            except Exception:
                pass

        # Labels
        raw_labels = getattr(record, "labels", None)
        if isinstance(raw_labels, dict):
            clamped_labels: dict[str, str] = {}
            for k, v in list(raw_labels.items())[:32]:
                if isinstance(k, str) and len(k) <= 64:
                    clamped_labels[k] = str(v)[:1024]
            if clamped_labels:
                entry["labels"] = clamped_labels

        if context:
            entry["context"] = context

        return entry
