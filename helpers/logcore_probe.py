"""
Logcore probe emission entry point.
Emits a test ERROR log to exercise the Logcore integration.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DEBUG", "True")
    os.environ.setdefault("DATABASE_URL", "file:memdb1?mode=memory&cache=shared")

    captured_entries: list[dict[str, Any]] = []

    try:
        import django

        django.setup()
    except Exception as exc:
        sys.stderr.write(f"Django setup notice: {exc}\n")

    from helpers.logcore import (
        LogcoreHandler,
        get_global_transport,
        parse_or_generate_traceparent,
        sampled_var,
        span_id_var,
        trace_id_var,
    )

    # Set active trace context for probe verification
    trace_id, span_id, sampled = parse_or_generate_traceparent(None)
    token_t = trace_id_var.set(trace_id)
    token_s = span_id_var.set(span_id)
    token_samp = sampled_var.set(sampled)

    # Hook transport to capture entries without making external network calls
    transport = get_global_transport()
    orig_enqueue = transport.enqueue

    def capture_enqueue(entry: dict[str, Any]) -> None:
        captured_entries.append(entry)

    transport.enqueue = capture_enqueue  # type: ignore[assignment]

    # Silence non-logcore console handlers for clean JSON stdout output
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        if not isinstance(h, LogcoreHandler):
            h.setLevel(logging.CRITICAL + 1)

    logger = logging.getLogger("helpers.logcore_probe")
    for h in list(logger.handlers):
        if not isinstance(h, LogcoreHandler):
            h.setLevel(logging.CRITICAL + 1)

    try:
        try:
            raise RuntimeError("Logcore probe test error for integration verification")
        except Exception:
            logger.error("Logcore probe verification error emitted", exc_info=True)
    finally:
        transport.enqueue = orig_enqueue  # type: ignore[assignment]
        trace_id_var.reset(token_t)
        span_id_var.reset(token_s)
        sampled_var.reset(token_samp)

    # Output captured JSON log entry (one JSON object per line)
    for entry in captured_entries:
        sys.stdout.write(json.dumps(entry) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
