"""Django middleware for logcore request tracing and context propagation."""

import asyncio
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, HttpResponse

from logcore.context import parse_traceparent, reset_context, reset_trace, set_context, set_trace


class LogcoreMiddleware:
    """Extracts W3C traceparent headers and binds request context for logging."""

    def __init__(self, get_response: Callable[[HttpRequest], Any]) -> None:
        self.get_response = get_response
        self.async_mode = asyncio.iscoroutinefunction(get_response)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self.async_mode:
            return self.__acall__(request)  # type: ignore[return-value]

        trace_token, ctx_token = self._setup_context(request)
        try:
            response = self.get_response(request)
            return response
        finally:
            self._cleanup_context(trace_token, ctx_token)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        trace_token, ctx_token = self._setup_context(request)
        try:
            response = await self.get_response(request)
            return response
        finally:
            self._cleanup_context(trace_token, ctx_token)

    def _setup_context(self, request: HttpRequest) -> tuple[Any, Any]:
        traceparent = request.headers.get("traceparent") if hasattr(request, "headers") else None
        if not traceparent and hasattr(request, "META"):
            traceparent = request.META.get("HTTP_TRACEPARENT")

        trace_token = None
        if traceparent:
            parsed_trace = parse_traceparent(traceparent)
            if parsed_trace:
                trace_token = set_trace(parsed_trace)

        req_ctx = {
            "http": {
                "method": getattr(request, "method", ""),
                "path": getattr(request, "path", ""),
            }
        }
        ctx_token = set_context(req_ctx)
        return trace_token, ctx_token

    def _cleanup_context(self, trace_token: Any, ctx_token: Any) -> None:
        if trace_token is not None:
            reset_trace(trace_token)
        if ctx_token is not None:
            reset_context(ctx_token)
