from __future__ import annotations

import re
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from logcore.context import current_trace

# W3C traceparent header regex: 00-<32 hex trace-id>-<16 hex parent-id>-<2 hex flags>
TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


class LogcoreTraceMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        header = request.META.get("HTTP_TRACEPARENT")
        token = None
        if header and isinstance(header, str):
            match = TRACEPARENT_RE.match(header.strip())
            if match:
                trace_id, span_id, flags = match.groups()
                if trace_id != "0" * 32 and span_id != "0" * 16:
                    sampled = bool(int(flags, 16) & 1)
                    token = current_trace.set(
                        {
                            "traceId": trace_id,
                            "spanId": span_id,
                            "sampled": sampled,
                        }
                    )

        try:
            response = self.get_response(request)
            return response
        finally:
            if token is not None:
                current_trace.reset(token)
            else:
                current_trace.set(None)
