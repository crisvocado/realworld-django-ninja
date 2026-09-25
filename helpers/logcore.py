"""
Logcore structured logging integration for Python/Django.
Implements HTTP transport adapter for Logcore gateway.
"""

from __future__ import annotations

import atexit
import collections
import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Trace context variables for request correlation
trace_id_var: contextvars.ContextVar[Any] = contextvars.ContextVar("logcore_trace_id", default=None)
span_id_var: contextvars.ContextVar[Any] = contextvars.ContextVar("logcore_span_id", default=None)
sampled_var: contextvars.ContextVar[Any] = contextvars.ContextVar("logcore_sampled", default=True)

TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
VALID_ENVS = {"prod", "staging", "dev", "test", "local"}


def normalize_env(env: str | None) -> str:
    """Normalize environment name to supported Logcore enums."""
    if not env:
        return "dev"
    env_clean = env.strip().lower()
    if env_clean in ("production", "prod"):
        return "prod"
    if env_clean in ("development", "dev"):
        return "dev"
    if env_clean in ("testing", "test"):
        return "test"
    if env_clean in ("stage", "staging"):
        return "staging"
    if env_clean == "local":
        return "local"
    return "dev"


def get_current_trace() -> dict[str, Any] | None:
    """Return active trace context if present."""
    trace_id = trace_id_var.get()
    span_id = span_id_var.get()
    sampled = sampled_var.get()
    if trace_id and span_id:
        return {
            "traceId": str(trace_id),
            "spanId": str(span_id),
            "sampled": bool(sampled),
        }
    return None


def parse_or_generate_traceparent(header: str | None) -> tuple[str, str, bool]:
    """Parse incoming W3C traceparent header or generate a new trace context."""
    if header:
        m = TRACEPARENT_RE.match(header.strip())
        if m:
            trace_id, _parent_span_id, flags_hex = m.groups()
            flags = int(flags_hex, 16)
            sampled = bool(flags & 1)
            new_span_id = secrets.token_hex(8)
            return trace_id, new_span_id, sampled

    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return trace_id, span_id, True


class LogcoreTraceMiddleware:
    """Django middleware to extract and propagate W3C traceparent context."""

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        header = None
        if hasattr(request, "headers"):
            header = request.headers.get("traceparent")
        if not header and hasattr(request, "META"):
            header = request.META.get("HTTP_TRACEPARENT")

        trace_id, span_id, sampled = parse_or_generate_traceparent(header)
        token_t = trace_id_var.set(trace_id)
        token_s = span_id_var.set(span_id)
        token_samp = sampled_var.set(sampled)

        try:
            response = self.get_response(request)
            if response is not None and hasattr(response, "__setitem__"):
                flags = "01" if sampled else "00"
                response["traceparent"] = f"00-{trace_id}-{span_id}-{flags}"
            return response
        finally:
            trace_id_var.reset(token_t)
            span_id_var.reset(token_s)
            sampled_var.reset(token_samp)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_file_in_app(filename: str | None, project_root: str | Path | None = None) -> bool:
    """Check if a file belongs to the application rather than dependencies/stdlib."""
    if not filename:
        return False
    if filename.startswith("<") or filename.endswith(">"):
        return False
    try:
        abs_path = str(Path(filename).resolve())
    except Exception:
        return False
    if "/.venv/" in abs_path or "/site-packages/" in abs_path or "/lib/python" in abs_path:
        return False
    root = str(Path(project_root).resolve()) if project_root else str(PROJECT_ROOT)
    try:
        return os.path.commonpath([abs_path, root]) == root
    except ValueError:
        return False


def extract_stack_frames(
    exc: BaseException | None,
    project_root: str | None = None,
) -> list[dict[str, Any]]:
    """
    Extract stack frames following exception chain to root cause.
    Places root cause first, innermost frame first (index 0).
    """
    if exc is None:
        return []

    # Follow exception chain to root
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not getattr(current, "__suppress_context__", False) and current.__context__ is not None:
            current = current.__context__
        else:
            break

    frames: list[dict[str, Any]] = []
    # Traverse from root cause up to outermost wrapper
    for ex in reversed(chain):
        tb = ex.__traceback__
        if not tb:
            continue
        # extract_tb gives outermost first, reverse to get innermost first
        tb_list = list(reversed(traceback.extract_tb(tb)))
        for f in tb_list:
            col = getattr(f, "colno", None)
            if col is None:
                col = 0
            frames.append(
                {
                    "function": f.name,
                    "file": f.filename,
                    "line": f.lineno or 0,
                    "column": col,
                    "inApp": is_file_in_app(f.filename, project_root),
                }
            )

    return frames[:50]


def format_iso_timestamp(created_ts: float | None = None) -> str:
    """Format timestamp as ISO-8601 UTC with milliseconds and Z suffix."""
    if created_ts is not None:
        dt = datetime.fromtimestamp(created_ts, UTC)
    else:
        dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def generate_insert_id(
    timestamp: str,
    service: str,
    severity: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Generate deterministic 32-char hex insert_id."""
    canonical_ctx = json.dumps(context or {}, sort_keys=True, separators=(",", ":"), default=str)
    raw = f"{timestamp}|{service}|{severity}|{message}|{canonical_ctx}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def build_logcore_entry(
    record: logging.LogRecord,
    service: str | None = None,
    env: str | None = None,
    project_root: str | None = None,
) -> dict[str, Any]:
    """Build a compliant Logcore log entry dictionary from a LogRecord."""
    svc = service or os.getenv("LOGCORE_SERVICE", "realworld-django-ninja")
    env_name = normalize_env(env or os.getenv("LOGCORE_ENV", "dev"))
    timestamp = format_iso_timestamp(getattr(record, "created", None))

    levelname = record.levelname.upper() if hasattr(record, "levelname") else "ERROR"
    severity = "CRITICAL" if levelname in ("CRITICAL", "FATAL") else "ERROR"

    message = str(record.getMessage())[:65536]

    # Context & Labels
    context: dict[str, Any] = {}
    ctx_val = getattr(record, "context", None)
    if isinstance(ctx_val, dict):
        for k, v in ctx_val.items():
            context[str(k)] = v

    labels: dict[str, str] = {}
    lbl_val = getattr(record, "labels", None)
    if isinstance(lbl_val, dict):
        for k, v in list(lbl_val.items())[:32]:
            labels[str(k)[:1024]] = str(v)[:1024]
    else:
        labels["logger"] = str(record.name)[:1024]

    insert_id = generate_insert_id(timestamp, svc, severity, message, context)

    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "severity": severity,
        "message": message,
        "service": svc,
        "env": env_name,
        "insert_id": insert_id,
    }

    # Trace
    trace = get_current_trace()
    if trace:
        entry["trace"] = trace

    # Error & parsed stack
    exc_info = record.exc_info
    exc_obj: BaseException | None = None
    if exc_info and isinstance(exc_info, tuple) and len(exc_info) >= 2:
        exc_obj = exc_info[1]

    if exc_obj is not None:
        frames = extract_stack_frames(exc_obj, project_root)
        entry["error"] = {
            "type": type(exc_obj).__name__,
            "message": str(exc_obj),
            "stack": frames,
        }
    elif getattr(record, "pathname", None):
        # Fallback single frame from caller info for ERROR records without exception
        is_in_app = is_file_in_app(record.pathname, project_root)
        entry["error"] = {
            "type": "Error",
            "message": message[:1024],
            "stack": [
                {
                    "function": getattr(record, "funcName", "unknown"),
                    "file": record.pathname,
                    "line": getattr(record, "lineno", 0) or 0,
                    "column": 0,
                    "inApp": is_in_app,
                }
            ],
        }

    if labels:
        entry["labels"] = labels
    if context:
        entry["context"] = context

    return entry


class LogcoreTransport:
    """
    Background batching HTTP transport for Logcore gateway.
    Handles queueing, async dispatch, retries, and circuit breaker.
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        max_buffer_size: int = 1000,
        batch_size: int = 20,
        flush_interval: float = 5.0,
        max_batch_per_request: int = 200,
    ) -> None:
        base_url = (url or os.getenv("LOGCORE_URL", "https://logcore-dev-352942961463.us-east4.run.app")).rstrip("/")
        self.endpoint = f"{base_url}/v1/logs"
        self.api_key = api_key if api_key is not None else os.getenv("LOGCORE_KEY", "")
        self.max_buffer_size = max_buffer_size
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_batch_per_request = max_batch_per_request

        self._buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_buffer_size)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._consecutive_failures = 0
        self._circuit_open = False
        self._running = True
        self._last_flush_time = time.monotonic()

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="LogcoreTransportWorker",
        )
        self._worker_thread.start()

    def enqueue(self, entry: dict[str, Any]) -> None:
        """Add an entry to the buffer without blocking."""
        with self._cond:
            if self._circuit_open:
                return
            self._buffer.append(entry)
            if len(self._buffer) >= self.batch_size:
                self._cond.notify_all()

    def _worker_loop(self) -> None:
        while self._running:
            batch: list[dict[str, Any]] = []
            with self._cond:
                while self._running and len(self._buffer) < self.batch_size:
                    elapsed = time.monotonic() - self._last_flush_time
                    timeout = max(0.1, self.flush_interval - elapsed)
                    if timeout <= 0 and self._buffer:
                        break
                    self._cond.wait(timeout=timeout)
                    if not self._running:
                        break
                    if time.monotonic() - self._last_flush_time >= self.flush_interval and self._buffer:
                        break

                if not self._running and not self._buffer:
                    break

                if self._buffer:
                    count = min(len(self._buffer), self.max_batch_per_request)
                    batch = [self._buffer.popleft() for _ in range(count)]
                    self._last_flush_time = time.monotonic()

            if batch:
                self._send_batch_with_retry(batch)

    def _send_batch_with_retry(self, batch: list[dict[str, Any]]) -> None:
        # Never send or report errors via application logger (self-exclusion)
        api_key = self.api_key or os.getenv("LOGCORE_KEY", "")
        if not api_key:
            return

        payload_bytes = json.dumps({"schema_version": 1, "entries": batch}).encode()

        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    self.endpoint,
                    data=payload_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": api_key,
                        "User-Agent": "logcore-python/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    status = resp.status
                    if 200 <= status < 300:
                        self._consecutive_failures = 0
                        self._circuit_open = False
                        return
                    if status == 429 or status >= 500:
                        pass
                    else:
                        sys.stderr.write(f"logcore permanent rejection: {status}\n")
                        sys.stderr.flush()
                        return
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status == 429 or status >= 500:
                    pass
                else:
                    try:
                        resp_body = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        resp_body = ""
                    sys.stderr.write(f"logcore permanent rejection: {status} {resp_body}\n")
                    sys.stderr.flush()
                    return
            except (urllib.error.URLError, TimeoutError, OSError, Exception):
                pass

            if attempt < 3:
                time.sleep((2**attempt) * 0.25)

        self._consecutive_failures += 1
        if self._consecutive_failures >= 10:
            self._circuit_open = True
            with self._cond:
                self._buffer.clear()
            sys.stderr.write("logcore circuit breaker opened after 10 consecutive failures\n")
            sys.stderr.flush()

    def flush(self, timeout: float = 5.0) -> None:
        """Synchronously flush all pending buffered entries."""
        while True:
            batch: list[dict[str, Any]] = []
            with self._cond:
                if not self._buffer:
                    break
                count = min(len(self._buffer), self.max_batch_per_request)
                batch = [self._buffer.popleft() for _ in range(count)]
                self._last_flush_time = time.monotonic()
            if batch:
                self._send_batch_with_retry(batch)
            else:
                break

    def stop(self, timeout: float = 5.0) -> None:
        """Stop background worker and flush remaining entries."""
        self._running = False
        with self._cond:
            self._cond.notify_all()
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
        self.flush(timeout=timeout)


_global_transport: LogcoreTransport | None = None
_global_transport_lock = threading.Lock()


def get_global_transport() -> LogcoreTransport:
    """Retrieve or initialize the global transport instance."""
    global _global_transport
    if _global_transport is None:
        with _global_transport_lock:
            if _global_transport is None:
                transport = LogcoreTransport()
                atexit.register(transport.stop)
                _global_transport = transport
    res = _global_transport
    if res is None:
        res = LogcoreTransport()
    return res


class LogcoreHandler(logging.Handler):
    """
    Logging handler that forwards ERROR+ records to the Logcore HTTP transport.
    """

    def __init__(
        self,
        level: int | str = logging.ERROR,
        transport: LogcoreTransport | None = None,
        url: str | None = None,
        api_key: str | None = None,
        service: str | None = None,
        env: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(level=level)
        self.service = service or os.getenv("LOGCORE_SERVICE", "realworld-django-ninja")
        self.env = normalize_env(env or os.getenv("LOGCORE_ENV", "dev"))
        self._enabled = enabled
        self._custom_transport = transport
        self._url = url
        self._api_key = api_key

    @property
    def transport(self) -> LogcoreTransport:
        if self._custom_transport is not None:
            return self._custom_transport
        return get_global_transport()

    def is_enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        val = os.getenv("LOGCORE_ENABLED", "true").strip().lower()
        return val in ("true", "1", "yes", "on")

    def emit(self, record: logging.LogRecord) -> None:
        """Forward record to Logcore transport if severity floor met."""
        try:
            if not self.is_enabled():
                return

            min_severity_name = os.getenv("LOGCORE_MIN_SEVERITY", "ERROR").upper()
            min_level = getattr(logging, min_severity_name, logging.ERROR)
            if not isinstance(min_level, int):
                min_level = logging.ERROR
            if record.levelno < min_level:
                return

            entry = build_logcore_entry(
                record=record,
                service=self.service,
                env=self.env,
            )
            self.transport.enqueue(entry)
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        try:
            self.transport.flush()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.flush()
        finally:
            super().close()
