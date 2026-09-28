"""Tests for Logcore structured logging integration."""

import json
import logging
import os
import unittest
from unittest.mock import MagicMock

import django

from logcore.client import LogcoreClient
from logcore.config import LogcoreConfig
from logcore.formatter import format_record
from logcore.handler import LogcoreHandler
from logcore.middleware import LogcoreTraceMiddleware
from logcore.trace import (
    TraceContext,
    generate_trace_context,
    get_current_trace_context,
    parse_traceparent,
    reset_trace_context,
    set_trace_context,
    trace_context_to_dict,
)


class TestLogcore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        os.environ.setdefault("DEBUG", "True")
        os.environ.setdefault("DATABASE_URL", "file:memdb1?mode=memory&cache=shared")
        django.setup()

    def test_default_config(self):
        config = LogcoreConfig()
        self.assertEqual(config.service, "realworld-django-ninja")
        self.assertIn(config.env, {"prod", "staging", "dev", "test", "local"})
        self.assertEqual(config.min_severity_level, logging.ERROR)

    def test_parse_valid_traceparent(self):
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ctx = parse_traceparent(header)
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(ctx.span_id, "00f067aa0ba902b7")
        self.assertTrue(ctx.sampled)

    def test_parse_invalid_traceparent(self):
        self.assertIsNone(parse_traceparent(None))
        self.assertIsNone(parse_traceparent(""))
        self.assertIsNone(parse_traceparent("invalid"))
        self.assertIsNone(parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"))
        self.assertIsNone(parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01"))
        self.assertIsNone(parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01"))

    def test_generate_trace_context(self):
        ctx = generate_trace_context()
        self.assertEqual(len(ctx.trace_id), 32)
        self.assertEqual(len(ctx.span_id), 16)
        self.assertTrue(ctx.sampled)

    def test_context_management(self):
        ctx = TraceContext(trace_id="a" * 32, span_id="b" * 16, sampled=True)
        token = set_trace_context(ctx)
        self.assertEqual(get_current_trace_context(), ctx)
        reset_trace_context(token)
        self.assertIsNone(get_current_trace_context())

    def test_trace_to_dict(self):
        ctx = TraceContext(trace_id="4bf92f3577b34da6a3ce929d0e0e4736", span_id="00f067aa0ba902b7", sampled=True)
        d = trace_context_to_dict(ctx)
        self.assertEqual(
            d,
            {
                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                "spanId": "00f067aa0ba902b7",
                "sampled": True,
            },
        )

    def test_format_basic_record(self):
        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="Something failed: %s",
            args=("database timeout",),
            exc_info=None,
        )
        entry = format_record(record)

        self.assertIn("timestamp", entry)
        self.assertTrue(entry["timestamp"].endswith("Z"))
        self.assertEqual(entry["severity"], "ERROR")
        self.assertEqual(entry["message"], "Something failed: database timeout")
        self.assertEqual(entry["service"], "realworld-django-ninja")
        self.assertEqual(entry["env"], "dev")
        self.assertRegex(entry["insert_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(entry["labels"], {"logger": "test.logger"})
        self.assertNotIn("error", entry)

    def test_format_record_with_exception_chain(self):
        try:
            try:
                raise TypeError("Cannot read properties of undefined (reading 'total')")
            except Exception as e:
                raise ValueError("checkout failed") from e
        except Exception:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="checkout.service",
            level=logging.ERROR,
            pathname=__file__,
            lineno=50,
            msg="checkout failed",
            args=(),
            exc_info=exc_info,
        )

        ctx = TraceContext(trace_id="4bf92f3577b34da6a3ce929d0e0e4736", span_id="00f067aa0ba902b7", sampled=True)
        token = set_trace_context(ctx)
        try:
            entry = format_record(record)
        finally:
            reset_trace_context(token)

        self.assertEqual(entry["severity"], "ERROR")
        self.assertEqual(entry["message"], "checkout failed")
        self.assertEqual(
            entry["trace"],
            {
                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                "spanId": "00f067aa0ba902b7",
                "sampled": True,
            },
        )

        error = entry["error"]
        self.assertEqual(error["type"], "ValueError")
        self.assertEqual(error["message"], "checkout failed")
        self.assertIsInstance(error["stack"], list)
        self.assertGreaterEqual(len(error["stack"]), 1)

        first_frame = error["stack"][0]
        self.assertIn("function", first_frame)
        self.assertIn("file", first_frame)
        self.assertIn("line", first_frame)
        self.assertIn("column", first_frame)
        self.assertIn("inApp", first_frame)
        self.assertIsInstance(first_frame["inApp"], bool)

    def test_golden_entry_fields(self):
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Test golden entry",
            args=(),
            exc_info=None,
        )
        entry = format_record(record)
        serialized = json.dumps(entry)
        deserialized = json.loads(serialized)
        self.assertEqual(deserialized["message"], "Test golden entry")

    def test_handler_emit(self):
        handler = LogcoreHandler()
        captured = []
        handler.client._custom_sender = lambda batch: captured.extend(batch)

        record = logging.LogRecord(
            name="test_handler",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Handler test message",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        handler.flush()
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["message"], "Handler test message")

    def test_enqueue_and_batching(self):
        batches = []
        client = LogcoreClient()
        client._custom_sender = lambda batch: batches.append(batch)

        for i in range(25):
            client.enqueue({"test": i})

        client.flush()
        total_delivered = sum(len(b) for b in batches)
        self.assertEqual(total_delivered, 25)
        client.shutdown()

    def test_buffer_cap_drops_oldest(self):
        client = LogcoreClient()
        for i in range(1050):
            client._buffer.append({"seq": i})

        self.assertEqual(len(client._buffer), 1000)
        self.assertEqual(client._buffer[0]["seq"], 50)
        self.assertEqual(client._buffer[-1]["seq"], 1049)

    def test_middleware_captures_traceparent(self):
        captured_trace = []

        def get_response(req):
            ctx = get_current_trace_context()
            if ctx:
                captured_trace.append(ctx)
            return MagicMock(status_code=200)

        middleware = LogcoreTraceMiddleware(get_response)
        request = MagicMock()
        request.headers = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
        request.META = {}

        _ = middleware(request)
        self.assertEqual(len(captured_trace), 1)
        self.assertEqual(captured_trace[0].trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(captured_trace[0].span_id, "00f067aa0ba902b7")
        self.assertTrue(captured_trace[0].sampled)
        self.assertIsNone(get_current_trace_context())


if __name__ == "__main__":
    unittest.main()
