"""Context management and W3C traceparent header parsing for logcore."""

import re
from contextvars import ContextVar, Token
from typing import Any

# Context variables for tracing and request context
_current_trace: ContextVar[dict[str, Any] | None] = ContextVar[dict[str, Any] | None](
    "logcore_current_trace", default=None
)
_current_context: ContextVar[dict[str, Any] | None] = ContextVar[dict[str, Any] | None](
    "logcore_current_context", default=None
)

# W3C traceparent regex: version(2 hex)-trace_id(32 hex)-parent_id(16 hex)-flags(2 hex)
TRACEPARENT_PATTERN = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$", re.IGNORECASE)


def parse_traceparent(header_value: str | None) -> dict[str, Any] | None:
    """Parse W3C traceparent header into traceId, spanId, sampled dict."""
    if not header_value:
        return None
    val = header_value.strip()
    match = TRACEPARENT_PATTERN.match(val)
    if not match:
        return None
    version, trace_id, span_id, flags_hex = match.groups()
    if version == "ff":
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    try:
        flags = int(flags_hex, 16)
    except ValueError:
        return None
    return {
        "traceId": trace_id.lower(),
        "spanId": span_id.lower(),
        "sampled": bool(flags & 1),
    }


def set_trace(trace_data: dict[str, Any] | None) -> Token[dict[str, Any] | None]:
    """Set the active trace in current context."""
    return _current_trace.set(trace_data)


def reset_trace(token: Token[dict[str, Any] | None]) -> None:
    """Reset the active trace."""
    _current_trace.reset(token)


def get_current_trace() -> dict[str, Any] | None:
    """Get the active trace in current context."""
    return _current_trace.get()


def set_context(context_data: dict[str, Any] | None) -> Token[dict[str, Any] | None]:
    """Set the active request context."""
    return _current_context.set(context_data)


def reset_context(token: Token[dict[str, Any] | None]) -> None:
    """Reset the active request context."""
    _current_context.reset(token)


def get_current_context() -> dict[str, Any] | None:
    """Get the active request context."""
    return _current_context.get()
