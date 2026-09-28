"""Django middleware to capture and propagate W3C traceparent headers for Logcore."""

from collections.abc import Callable
from typing import Any

from logcore.trace import (
    generate_trace_context,
    parse_traceparent,
    reset_trace_context,
    set_trace_context,
)


class LogcoreTraceMiddleware:
    """Django middleware to capture and propagate W3C traceparent headers."""

    def __init__(self, get_response: Callable[..., Any]) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        traceparent = None
        headers = getattr(request, "headers", None)
        if headers is not None and hasattr(headers, "get"):
            traceparent = headers.get("traceparent")
        if not traceparent and hasattr(request, "META") and isinstance(request.META, dict):
            traceparent = request.META.get("HTTP_TRACEPARENT")

        trace_ctx = parse_traceparent(traceparent)
        if trace_ctx is None:
            trace_ctx = generate_trace_context()

        token = set_trace_context(trace_ctx)
        try:
            return self.get_response(request)
        finally:
            reset_trace_context(token)
