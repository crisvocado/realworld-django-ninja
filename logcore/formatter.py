"""Log formatting and structured entry building for logcore."""

import hashlib
import json
import logging
import os
import re
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from logcore.context import get_current_context, get_current_trace

VALID_ENVS = {"prod", "staging", "dev", "test", "local"}
ENV_ALIASES = {
    "production": "prod",
    "development": "dev",
    "testing": "test",
}
SERVICE_REGEX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def get_service_name() -> str:
    """Read and validate the service name from environment."""
    raw = os.getenv("LOGCORE_SERVICE", "realworld-django-ninja").strip()
    if SERVICE_REGEX.match(raw):
        return raw
    cleaned = re.sub(r"[^a-z0-9._-]", "-", raw.lower()).strip(".-_")
    return cleaned[:63] if cleaned else "realworld-django-ninja"


def get_env_name() -> str:
    """Read and validate the environment name."""
    raw = os.getenv("LOGCORE_ENV", "dev").strip().lower()
    if raw in VALID_ENVS:
        return raw
    if raw in ENV_ALIASES:
        return ENV_ALIASES[raw]
    return "dev"


def normalize_severity(levelname: str) -> str:
    """Normalize log level name to standard logcore severity."""
    upper = levelname.upper()
    if upper == "FATAL":
        return "CRITICAL"
    if upper == "WARN":
        return "WARNING"
    return upper


def compute_insert_id(
    timestamp: str,
    service: str,
    severity: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Compute a deterministic 32-hex-character insert_id."""
    ctx_str = json.dumps(context, sort_keys=True, separators=(",", ":")) if context else ""
    raw = f"{timestamp}|{service}|{severity}|{message}|{ctx_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_in_app(filename: str, base_dir: Path | None = None) -> bool:
    """Determine whether a frame source file belongs to application code."""
    if not filename:
        return False
    if filename.startswith("<") and filename.endswith(">"):
        return False
    norm_path = str(Path(filename).resolve())
    for excluded in (
        "/site-packages/",
        "/dist-packages/",
        "/.venv/",
        "/venv/",
        "/.tox/",
        "/lib/python",
        "/usr/local/lib/",
        "/usr/lib/",
    ):
        if excluded in norm_path:
            return False
    if base_dir:
        try:
            rel = os.path.relpath(norm_path, str(base_dir))
            return not rel.startswith("..") and not Path(rel).is_absolute()
        except ValueError:
            return False
    return True


def format_exception(exc: BaseException, base_dir: Path | None = None) -> dict[str, Any]:
    """Format an exception into structured error object with parsed frames."""
    exc_type_name = exc.__class__.__name__
    exc_message = str(exc)

    # Follow exception chain to root cause
    chain: list[BaseException] = []
    curr: BaseException | None = exc
    visited: set[int] = set()
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        chain.append(curr)
        if curr.__cause__ is not None:
            curr = curr.__cause__
        elif not curr.__suppress_context__ and curr.__context__ is not None:
            curr = curr.__context__
        else:
            break

    # Root cause first
    root_first_chain = list(reversed(chain))

    stack: list[dict[str, Any]] = []
    for e in root_first_chain:
        tb = e.__traceback__
        if tb is None:
            continue
        # Innermost frame first
        tb_frames = list(reversed(traceback.extract_tb(tb)))
        for frame in tb_frames:
            if len(stack) >= 50:
                break
            colno = getattr(frame, "colno", 0)
            if colno is None:
                colno = 0
            stack.append(
                {
                    "function": frame.name,
                    "file": frame.filename,
                    "line": frame.lineno,
                    "column": colno,
                    "inApp": is_in_app(frame.filename, base_dir),
                }
            )
        if len(stack) >= 50:
            break

    return {
        "type": exc_type_name,
        "message": exc_message,
        "stack": stack,
    }


def format_entry(record: logging.LogRecord, base_dir: Path | None = None) -> dict[str, Any]:
    """Format a logging.LogRecord into a structured logcore wire payload."""
    # Timestamp with millisecond precision and UTC Z suffix
    dt = datetime.fromtimestamp(record.created, tz=UTC)
    timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    severity = normalize_severity(record.levelname)

    try:
        raw_msg = record.getMessage()
    except Exception:
        raw_msg = str(record.msg)
    message = raw_msg[:65536]

    service = get_service_name()
    env = get_env_name()

    # Context & Labels
    context: dict[str, Any] = {}
    current_ctx = get_current_context()
    if current_ctx and isinstance(current_ctx, dict):
        for k, v in current_ctx.items():
            context[str(k)] = v
    if hasattr(record, "context") and isinstance(record.context, dict):
        for k, v in record.context.items():
            context[str(k)] = v

    labels: dict[str, str] = {}
    if hasattr(record, "labels") and isinstance(record.labels, dict):
        for k, v in record.labels.items():
            if len(labels) >= 32:
                break
            labels[str(k)] = str(v)[:1024]

    insert_id = compute_insert_id(timestamp, service, severity, message, context if context else None)

    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "severity": severity,
        "message": message,
        "service": service,
        "env": env,
        "insert_id": insert_id,
    }

    # Trace
    trace = get_current_trace()
    if hasattr(record, "trace") and isinstance(record.trace, dict):
        trace = record.trace
    if trace:
        entry["trace"] = trace

    # Error
    exc_info = record.exc_info
    if isinstance(exc_info, tuple) and len(exc_info) >= 2 and exc_info[0] is not None:
        exc_type = exc_info[0]
        exc_value = exc_info[1]
        if isinstance(exc_value, BaseException):
            entry["error"] = format_exception(exc_value, base_dir)
        elif isinstance(exc_type, type) and issubclass(exc_type, BaseException):
            entry["error"] = {
                "type": exc_type.__name__,
                "message": str(exc_value) if exc_value is not None else "",
                "stack": [],
            }

    if labels:
        entry["labels"] = labels

    if context:
        entry["context"] = context

    return entry
