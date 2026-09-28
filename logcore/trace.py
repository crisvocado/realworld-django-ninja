"""W3C traceparent parsing and context tracking for Logcore."""

import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

_TRACEPARENT_RE = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
    re.IGNORECASE,
)
_INVALID_TRACE_ID = "0" * 32
_INVALID_SPAN_ID = "0" * 16


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    sampled: bool


_trace_context_var: ContextVar[TraceContext | None] = ContextVar("logcore_trace_context")


def parse_traceparent(header_value: str | None) -> TraceContext | None:
    if not header_value or not isinstance(header_value, str):
        return None

    match = _TRACEPARENT_RE.match(header_value.strip())
    if not match:
        return None

    version, trace_id, span_id, flags = match.groups()
    if version == "ff" or trace_id == _INVALID_TRACE_ID or span_id == _INVALID_SPAN_ID:
        return None

    try:
        flags_int = int(flags, 16)
        sampled = bool(flags_int & 1)
    except ValueError:
        sampled = True

    return TraceContext(
        trace_id=trace_id.lower(),
        span_id=span_id.lower(),
        sampled=sampled,
    )


def generate_trace_context() -> TraceContext:
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return TraceContext(trace_id=trace_id, span_id=span_id, sampled=True)


def get_current_trace_context() -> TraceContext | None:
    try:
        return _trace_context_var.get()
    except LookupError:
        return None


def set_trace_context(trace_ctx: TraceContext | None) -> Token[TraceContext | None]:
    return _trace_context_var.set(trace_ctx)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    _trace_context_var.reset(token)


def trace_context_to_dict(ctx: TraceContext | None) -> dict[str, Any] | None:
    if ctx is None:
        return None
    return {
        "traceId": ctx.trace_id,
        "spanId": ctx.span_id,
        "sampled": ctx.sampled,
    }
