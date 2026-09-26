"""Logcore structured logging integration for Django."""

import atexit
import contextvars
import datetime
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Default configuration
LOGCORE_URL_DEFAULT = "https://logcore-dev-352942961463.us-east4.run.app"
LOGCORE_SERVICE_DEFAULT = "realworld-django-ninja"
LOGCORE_ENV_DEFAULT = "dev"
LOGCORE_MIN_SEVERITY_DEFAULT = "ERROR"

VALID_ENVS = {"prod", "staging", "dev", "test", "local"}

# Context variables for request and trace correlation
_current_trace_context: contextvars.ContextVar[Any] = contextvars.ContextVar("logcore_trace_context", default=None)
_current_request_context: contextvars.ContextVar[Any] = contextvars.ContextVar("logcore_request_context", default=None)


def get_trace_context() -> dict[str, Any] | None:
    """Retrieve the current trace context for the running task/thread."""
    return _current_trace_context.get()


def set_trace_context(trace: dict[str, Any] | None) -> contextvars.Token:
    """Set the current trace context."""
    return _current_trace_context.set(trace)


def get_request_context() -> dict[str, Any] | None:
    """Retrieve the current request context for the running task/thread."""
    return _current_request_context.get()


def set_request_context(ctx: dict[str, Any] | None) -> contextvars.Token:
    """Set the current request context."""
    return _current_request_context.set(ctx)


def get_config() -> dict[str, Any]:
    """Load configuration from environment variables."""
    url = os.getenv("LOGCORE_URL", LOGCORE_URL_DEFAULT).rstrip("/")
    key = os.getenv("LOGCORE_KEY", "")
    enabled_str = os.getenv("LOGCORE_ENABLED", "true").lower()
    enabled = enabled_str in ("true", "1", "yes")

    service = os.getenv("LOGCORE_SERVICE", LOGCORE_SERVICE_DEFAULT).lower()
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,62}$", service):
        service = LOGCORE_SERVICE_DEFAULT

    raw_env = os.getenv("LOGCORE_ENV", LOGCORE_ENV_DEFAULT).lower()
    if raw_env == "production":
        env = "prod"
    elif raw_env == "development":
        env = "dev"
    elif raw_env in VALID_ENVS:
        env = raw_env
    else:
        env = "dev"

    min_severity = os.getenv("LOGCORE_MIN_SEVERITY", LOGCORE_MIN_SEVERITY_DEFAULT).upper()
    if min_severity not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "FATAL"):
        min_severity = "ERROR"

    return {
        "url": url,
        "key": key,
        "enabled": enabled,
        "service": service,
        "env": env,
        "min_severity": min_severity,
    }


def parse_traceparent(header_val: str | None) -> dict[str, Any] | None:
    """Parse a W3C traceparent header into trace context."""
    if not header_val or not isinstance(header_val, str):
        return None
    parts = header_val.strip().split("-")
    if len(parts) >= 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        try:
            flags = int(parts[3], 16)
            sampled = bool(flags & 1)
        except ValueError:
            sampled = False
        return {
            "traceId": parts[1].lower(),
            "spanId": parts[2].lower(),
            "sampled": sampled,
        }
    return None


def is_in_app(filepath: str, base_dir: Path | None = None) -> bool:
    """Determine if a file path belongs to application source code."""
    if not filepath:
        return False
    try:
        resolved_file = Path(filepath).resolve()
        if base_dir is None:
            try:
                from django.conf import settings

                base_dir = getattr(settings, "BASE_DIR", Path.cwd())
            except Exception:
                base_dir = Path.cwd()
        resolved_base = Path(base_dir).resolve()

        if not resolved_file.is_relative_to(resolved_base):
            return False

        rel_parts = resolved_file.relative_to(resolved_base).parts
        for part in rel_parts:
            if part in (
                ".venv",
                "venv",
                "site-packages",
                "node_modules",
                ".git",
                ".tox",
                ".nox",
                "dist",
                "build",
            ):
                return False
        return True
    except Exception:
        return False


def extract_error_info(exc_info: Any, base_dir: Path | None = None) -> dict[str, Any] | None:
    """Extract parsed error structure following exception chains to root."""
    if not exc_info:
        return None

    exc_type, exc_value, exc_tb = exc_info if isinstance(exc_info, tuple) else (None, None, None)

    if exc_value is None and isinstance(exc_type, type) and issubclass(exc_type, BaseException):
        error_type = exc_type.__name__
        error_message = ""
    elif isinstance(exc_value, BaseException):
        error_type = exc_value.__class__.__name__
        error_message = str(exc_value)
    elif exc_type is not None:
        error_type = str(exc_type)
        error_message = str(exc_value or "")
    else:
        return None

    frames_list: list[dict[str, Any]] = []

    if isinstance(exc_value, BaseException):
        # Follow exception chain to root
        chain: list[BaseException] = []
        curr: BaseException | None = exc_value
        seen: set[int] = set()
        while curr is not None and id(curr) not in seen:
            seen.add(id(curr))
            chain.append(curr)
            if curr.__cause__ is not None:
                curr = curr.__cause__
            elif curr.__context__ is not None and not getattr(curr, "__suppress_context__", False):
                curr = curr.__context__
            else:
                break

        # Put root exception first so its innermost throwing frame is at index 0
        chain_root_first = list(reversed(chain))

        for exc_item in chain_root_first:
            tb = exc_item.__traceback__
            if tb is not None:
                extracted = traceback.extract_tb(tb)
                # Innermost first within each exception
                for f in reversed(extracted):
                    col = getattr(f, "colno", 0)
                    frames_list.append(
                        {
                            "function": f.name or "<anon>",
                            "file": f.filename,
                            "line": f.lineno or 0,
                            "column": col if col is not None else 0,
                            "inApp": is_in_app(f.filename, base_dir),
                        }
                    )
    elif exc_tb is not None:
        extracted = traceback.extract_tb(exc_tb)
        for f in reversed(extracted):
            col = getattr(f, "colno", 0)
            frames_list.append(
                {
                    "function": f.name or "<anon>",
                    "file": f.filename,
                    "line": f.lineno or 0,
                    "column": col if col is not None else 0,
                    "inApp": is_in_app(f.filename, base_dir),
                }
            )

    # Clamp stack frames to 50
    stack = frames_list[:50]

    return {
        "type": error_type,
        "message": error_message[:65536],
        "stack": stack,
    }


def format_entry(
    record: logging.LogRecord,
    config: dict[str, Any] | None = None,
    trace_context: dict[str, Any] | None = None,
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Format a logging.LogRecord into a Logcore entry dict matching wire_shape and golden_entry."""
    if config is None:
        config = get_config()

    created_dt = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
    timestamp = created_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    severity = record.levelname.upper()
    if severity not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "FATAL"):
        severity = "ERROR"

    raw_message = record.getMessage()
    message = raw_message[:65536] if raw_message else ""

    service = config["service"]
    env = config["env"]

    # Context collection
    context: dict[str, Any] = {}
    req_ctx = request_context or get_request_context()
    if req_ctx and isinstance(req_ctx, dict):
        context.update(req_ctx)
    record_ctx = getattr(record, "context", None)
    if isinstance(record_ctx, dict):
        for k, v in record_ctx.items():
            context[str(k)] = v

    # Trace correlation
    trace = trace_context or get_trace_context()
    if not trace and hasattr(record, "trace") and isinstance(record.trace, dict):
        trace = record.trace

    # Error details
    error = None
    if record.exc_info:
        error = extract_error_info(record.exc_info)

    # Labels
    labels: dict[str, str] = {
        "logger": str(record.name)[:1024],
    }
    if hasattr(record, "labels") and isinstance(record.labels, dict):
        for k, v in record.labels.items():
            if len(labels) < 32:
                labels[str(k)[:1024]] = str(v)[:1024]

    # Deterministic insert_id
    try:
        canonical_ctx = json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        canonical_ctx = "{}"
    raw_id = f"{timestamp}|{service}|{severity}|{message}|{canonical_ctx}"
    insert_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]

    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "severity": severity,
        "message": message,
        "service": service,
        "env": env,
        "insert_id": insert_id,
    }

    if trace:
        entry["trace"] = trace
    if error:
        entry["error"] = error
    if labels:
        entry["labels"] = labels
    if context:
        entry["context"] = context

    return entry


class LogcoreTransport:
    """Thread-safe background batching HTTP transport with circuit breaker and retries."""

    def __init__(self) -> None:
        self.config = get_config()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1000)
        self._consecutive_failures = 0
        self._circuit_broken = False
        self._lock = threading.Lock()
        self._shutdown_flag = threading.Event()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="logcore-transport")
        self._worker_thread.start()

    def enqueue(self, entry: dict[str, Any]) -> None:
        """Enqueue an entry without blocking calling thread."""
        if self._shutdown_flag.is_set():
            return

        if self._circuit_broken:
            return

        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            # Buffer cap reached: drop oldest entry to preserve newest
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(entry)
            except queue.Full:
                pass

    def _worker_loop(self) -> None:
        """Background thread collecting batches and delivering over HTTP."""
        batch: list[dict[str, Any]] = []
        last_flush_time = time.monotonic()

        while not self._shutdown_flag.is_set():
            timeout = max(0.1, 5.0 - (time.monotonic() - last_flush_time))
            try:
                item = self._queue.get(timeout=timeout)
                if item is None:
                    # Sentinel for flush/shutdown
                    break
                batch.append(item)
            except queue.Empty:
                pass

            now = time.monotonic()
            if len(batch) >= 20 or (batch and (now - last_flush_time) >= 5.0):
                self._flush_batch(batch[:200])
                batch = batch[200:]
                last_flush_time = now

        # Drain remaining items on exit
        if batch:
            self._flush_batch(batch[:200])
        while not self._queue.empty():
            remaining: list[dict[str, Any]] = []
            while len(remaining) < 200:
                try:
                    item = self._queue.get_nowait()
                    if item is not None:
                        remaining.append(item)
                except queue.Empty:
                    break
            if remaining:
                self._flush_batch(remaining)

    def _flush_batch(self, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return True

        if not self.config["enabled"] or not self.config["key"]:
            return True

        success = self._send_http(batch)
        with self._lock:
            if success:
                self._consecutive_failures = 0
                self._circuit_broken = False
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 10:
                    self._circuit_broken = True
                    # Empty queue on circuit breaker trip
                    try:
                        while True:
                            self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    sys.stderr.write("[Logcore] Circuit breaker tripped after 10 consecutive failures. Pausing.\n")
                    sys.stderr.flush()
        return success

    def _send_http(self, batch: list[dict[str, Any]]) -> bool:
        url = f"{self.config['url'].rstrip('/')}/v1/logs"
        payload = json.dumps({"schema_version": 1, "entries": batch}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config["key"],
            "User-Agent": "logcore-python",
        }

        for attempt in range(4):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except urllib.error.HTTPError as err:
                status_code = err.code
                err_body = ""
                try:
                    err_body = err.read().decode("utf-8", errors="replace")
                except Exception:
                    pass

                # Permanent failure: 4xx (except 429) should not be retried
                if 400 <= status_code < 500 and status_code != 429:
                    sys.stderr.write(f"[Logcore] Server rejected batch ({status_code}): {err_body}\n")
                    sys.stderr.flush()
                    return False
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                if attempt == 3:
                    sys.stderr.write(f"[Logcore] Network delivery error after 3 retries: {err}\n")
                    sys.stderr.flush()
                    return False
            except Exception as err:
                sys.stderr.write(f"[Logcore] Unexpected delivery error: {err}\n")
                sys.stderr.flush()
                return False

            if attempt < 3:
                backoff = (2**attempt) * 0.25
                time.sleep(backoff)

        return False

    def flush(self, timeout: float = 5.0) -> None:
        """Synchronously flush remaining queued entries."""
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Shutdown the transport background worker."""
        self._shutdown_flag.set()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)


_global_transport: LogcoreTransport | None = None
_transport_lock = threading.Lock()


def get_transport() -> LogcoreTransport:
    """Get or initialize singleton Logcore transport."""
    global _global_transport
    if _global_transport is None:
        with _transport_lock:
            if _global_transport is None:
                _global_transport = LogcoreTransport()
    transport = _global_transport
    if transport is None:
        transport = LogcoreTransport()
    return transport


def flush(timeout: float = 5.0) -> None:
    """Flush pending log entries globally."""
    if _global_transport is not None:
        _global_transport.flush(timeout=timeout)


def shutdown(timeout: float = 5.0) -> None:
    """Shut down global transport."""
    if _global_transport is not None:
        _global_transport.shutdown(timeout=timeout)


atexit.register(shutdown)


class LogcoreHandler(logging.Handler):
    """Standard logging.Handler attached to root and framework loggers."""

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Self-exclusion
            if record.name.startswith("logcore"):
                return

            transport = get_transport()
            min_severity_name = transport.config.get("min_severity", "ERROR")
            min_severity_val = getattr(logging, min_severity_name, logging.ERROR)
            min_severity = min_severity_val if isinstance(min_severity_val, int) else logging.ERROR

            if record.levelno < min_severity:
                return

            entry = format_entry(
                record,
                config=transport.config,
                trace_context=get_trace_context(),
                request_context=get_request_context(),
            )
            transport.enqueue(entry)
        except Exception as exc:
            # Logging failure must never break the application
            try:
                sys.stderr.write(f"[Logcore] Emission failed: {exc}\n")
                sys.stderr.flush()
            except Exception:
                pass

    def flush(self) -> None:
        flush()

    def close(self) -> None:
        self.flush()
        super().close()


class LogcoreMiddleware:
    """Django middleware to capture traceparent and request context."""

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        trace_header = request.META.get("HTTP_TRACEPARENT") or getattr(request, "headers", {}).get("traceparent")
        trace = parse_traceparent(trace_header)
        trace_token = set_trace_context(trace)

        user_id = None
        if hasattr(request, "user") and getattr(request.user, "is_authenticated", False):
            user_id = str(getattr(request.user, "id", getattr(request.user, "pk", "")))

        req_ctx: dict[str, Any] = {
            "http": {
                "method": getattr(request, "method", "UNKNOWN"),
                "path": getattr(request, "path", ""),
            }
        }
        if hasattr(request, "build_absolute_uri"):
            try:
                req_ctx["http"]["url"] = request.build_absolute_uri()
            except Exception:
                req_ctx["http"]["url"] = getattr(request, "path", "")
        if user_id:
            req_ctx["user"] = {"id": user_id}

        req_token = set_request_context(req_ctx)

        try:
            return self.get_response(request)
        finally:
            _current_trace_context.reset(trace_token)
            _current_request_context.reset(req_token)
