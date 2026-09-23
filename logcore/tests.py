"""Tests for logcore structured logging integration."""

import logging
import urllib.error
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from django.http import HttpRequest, HttpResponse
from django.test import SimpleTestCase

from logcore.context import (
    get_current_context,
    get_current_trace,
    parse_traceparent,
    reset_context,
    reset_trace,
    set_context,
    set_trace,
)
from logcore.formatter import (
    compute_insert_id,
    format_entry,
    format_exception,
    get_env_name,
    get_service_name,
    is_in_app,
    normalize_severity,
)
from logcore.middleware import LogcoreMiddleware
from logcore.probe import main as probe_main
from logcore.transport import LogcoreTransport


class LogcoreContextTests(SimpleTestCase):
    """Tests for W3C traceparent parsing and context propagation."""

    def test_parse_traceparent_valid(self) -> None:
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        trace = parse_traceparent(header)
        self.assertIsNotNone(trace)
        assert trace is not None
        self.assertEqual(trace["traceId"], "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(trace["spanId"], "00f067aa0ba902b7")
        self.assertTrue(trace["sampled"])

    def test_parse_traceparent_unsampled(self) -> None:
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
        trace = parse_traceparent(header)
        self.assertIsNotNone(trace)
        assert trace is not None
        self.assertEqual(trace["traceId"], "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(trace["spanId"], "00f067aa0ba902b7")
        self.assertFalse(trace["sampled"])

    def test_parse_traceparent_invalid(self) -> None:
        self.assertIsNone(parse_traceparent(None))
        self.assertIsNone(parse_traceparent(""))
        self.assertIsNone(parse_traceparent("invalid"))
        self.assertIsNone(parse_traceparent("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"))
        self.assertIsNone(parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01"))

    def test_context_set_and_reset(self) -> None:
        trace_data = {"traceId": "abc", "spanId": "def", "sampled": True}
        token = set_trace(trace_data)
        self.assertEqual(get_current_trace(), trace_data)
        reset_trace(token)
        self.assertIsNone(get_current_trace())

        ctx_data = {"user_id": 123}
        ctx_token = set_context(ctx_data)
        self.assertEqual(get_current_context(), ctx_data)
        reset_context(ctx_token)
        self.assertIsNone(get_current_context())


class LogcoreFormatterTests(SimpleTestCase):
    """Tests for structured entry formatting and error extraction."""

    def test_normalize_severity(self) -> None:
        self.assertEqual(normalize_severity("error"), "ERROR")
        self.assertEqual(normalize_severity("FATAL"), "CRITICAL")
        self.assertEqual(normalize_severity("WARN"), "WARNING")
        self.assertEqual(normalize_severity("info"), "INFO")

    def test_compute_insert_id(self) -> None:
        ts = "2026-08-17T18:30:00.000Z"
        insert_id = compute_insert_id(ts, "realworld", "ERROR", "test message", {"key": "val"})
        self.assertEqual(len(insert_id), 32)
        # Verify hex
        int(insert_id, 16)
        # Deterministic
        insert_id2 = compute_insert_id(ts, "realworld", "ERROR", "test message", {"key": "val"})
        self.assertEqual(insert_id, insert_id2)

    def test_is_in_app(self) -> None:
        self.assertFalse(is_in_app("<string>"))
        self.assertFalse(is_in_app("/path/to/.venv/lib/python3.12/site-packages/django/core/handlers.py"))
        self.assertTrue(is_in_app("/workspace/realworld-django-ninja/apps/articles/api.py"))

    def test_format_exception_root_cause_first_and_innermost_first(self) -> None:
        def raise_root() -> None:
            raise ValueError("Root cause error")

        def raise_wrapper() -> None:
            try:
                raise_root()
            except ValueError as root_exc:
                raise RuntimeError("Wrapper error") from root_exc

        try:
            raise_wrapper()
        except RuntimeError as exc:
            formatted = format_exception(exc)

            self.assertEqual(formatted["type"], "RuntimeError")
            self.assertEqual(formatted["message"], "Wrapper error")
            stack = formatted["stack"]
            self.assertTrue(len(stack) > 0)
            # The root cause innermost frame should be at index 0
            self.assertEqual(stack[0]["function"], "raise_root")
            self.assertIn("line", stack[0])
            self.assertIn("column", stack[0])
            self.assertIn("inApp", stack[0])

    def test_format_entry(self) -> None:
        logger = logging.getLogger("test_logger")
        record = logger.makeRecord(
            name="test_logger",
            level=logging.ERROR,
            fn="test_fn",
            lno=10,
            msg="checkout failed",
            args=(),
            exc_info=None,
        )

        trace_token = set_trace(
            {"traceId": "4bf92f3577b34da6a3ce929d0e0e4736", "spanId": "00f067aa0ba902b7", "sampled": True}
        )
        ctx_token = set_context({"request_id": "req-123"})
        try:
            entry = format_entry(record)
            self.assertEqual(entry["severity"], "ERROR")
            self.assertEqual(entry["message"], "checkout failed")
            self.assertEqual(entry["service"], get_service_name())
            self.assertEqual(entry["env"], get_env_name())
            self.assertEqual(len(entry["insert_id"]), 32)
            self.assertEqual(entry["trace"]["traceId"], "4bf92f3577b34da6a3ce929d0e0e4736")
            self.assertEqual(entry["context"]["request_id"], "req-123")
        finally:
            reset_trace(trace_token)
            reset_context(ctx_token)


class LogcoreTransportTests(SimpleTestCase):
    """Tests for LogcoreTransport batching, retries, and circuit breaker."""

    @patch("urllib.request.urlopen")
    def test_deliver_batch_success(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        transport = LogcoreTransport(enabled=True)
        transport.shutdown()  # Stop worker loop to test synchronous deliver_batch

        entry = {"timestamp": datetime.now(UTC).isoformat() + "Z", "severity": "ERROR", "message": "Test"}
        result = transport._deliver_batch([entry])
        self.assertTrue(result)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    def test_deliver_batch_permanent_rejection_4xx(self, mock_urlopen: MagicMock) -> None:
        err = urllib.error.HTTPError(
            url="http://example.com",
            code=422,
            msg="Unprocessable Entity",
            hdrs={},  # type: ignore[arg-type]
            fp=None,
        )
        err.read = MagicMock(return_value=b'{"detail": "Invalid schema"}')  # type: ignore[method-assign]
        mock_urlopen.side_effect = err

        transport = LogcoreTransport(enabled=True)
        transport.shutdown()

        entry = {"timestamp": "2026-08-17T18:30:00.000Z", "severity": "ERROR", "message": "Test"}
        result = transport._deliver_batch([entry])
        self.assertFalse(result)
        # Should not retry 4xx errors
        self.assertEqual(mock_urlopen.call_count, 1)


class LogcoreMiddlewareTests(SimpleTestCase):
    """Tests for LogcoreMiddleware."""

    def test_middleware_sync_request(self) -> None:
        def dummy_get_response(request: HttpRequest) -> HttpResponse:
            # During request processing, trace and context should be available
            trace = get_current_trace()
            self.assertIsNotNone(trace)
            assert trace is not None
            self.assertEqual(trace["traceId"], "4bf92f3577b34da6a3ce929d0e0e4736")
            ctx = get_current_context()
            self.assertIsNotNone(ctx)
            assert ctx is not None
            self.assertEqual(ctx["http"]["path"], "/test-path")
            return HttpResponse("OK")

        middleware = LogcoreMiddleware(dummy_get_response)
        request = HttpRequest()
        request.method = "GET"
        request.path = "/test-path"
        request.headers = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}  # type: ignore[misc]

        response = middleware(request)
        self.assertEqual(response.status_code, 200)
        # Context should be cleaned up
        self.assertIsNone(get_current_trace())
        self.assertIsNone(get_current_context())


class LogcoreProbeTests(SimpleTestCase):
    """Tests for logcore probe module."""

    def test_probe_emits_error(self) -> None:
        import io
        import json
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            probe_main()

        output = buf.getvalue().strip()
        self.assertTrue(output)
        entry = json.loads(output)
        self.assertEqual(entry["severity"], "ERROR")
        self.assertIn("probe", entry["message"].lower())
        self.assertEqual(entry["service"], get_service_name())
        self.assertEqual(entry["env"], get_env_name())
        self.assertEqual(len(entry["insert_id"]), 32)
