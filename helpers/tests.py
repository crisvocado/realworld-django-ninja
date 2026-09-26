"""Tests for logcore structured logging integration."""

import json
import logging
import unittest
from unittest.mock import MagicMock, patch

from helpers.logcore import (
    LogcoreMiddleware,
    LogcoreTransport,
    extract_error_info,
    format_entry,
    get_request_context,
    get_trace_context,
    is_in_app,
    parse_traceparent,
)


class LogcoreTests(unittest.TestCase):
    def test_parse_traceparent_valid(self):
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        res = parse_traceparent(header)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["traceId"], "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(res["spanId"], "00f067aa0ba902b7")
        self.assertTrue(res["sampled"])

    def test_parse_traceparent_unsampled(self):
        header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
        res = parse_traceparent(header)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertFalse(res["sampled"])

    def test_parse_traceparent_invalid(self):
        self.assertIsNone(parse_traceparent(None))
        self.assertIsNone(parse_traceparent(""))
        self.assertIsNone(parse_traceparent("invalid-header"))

    def test_is_in_app(self):
        self.assertTrue(is_in_app(__file__))
        self.assertFalse(
            is_in_app("/workspace/realworld-django-ninja/.venv/lib/python3.12/site-packages/django/core.py")
        )
        self.assertFalse(is_in_app("/usr/lib/python3.12/os.py"))

    def test_format_entry_basic(self):
        record = logging.LogRecord(
            name="test_logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="test error message",
            args=(),
            exc_info=None,
        )
        config = {
            "service": "realworld-django-ninja",
            "env": "dev",
            "url": "https://logcore-dev-352942961463.us-east4.run.app",
            "key": "test-key",
            "enabled": True,
            "min_severity": "ERROR",
        }
        entry = format_entry(record, config=config)

        self.assertEqual(entry["severity"], "ERROR")
        self.assertEqual(entry["message"], "test error message")
        self.assertEqual(entry["service"], "realworld-django-ninja")
        self.assertEqual(entry["env"], "dev")
        self.assertEqual(len(entry["insert_id"]), 32)
        self.assertIn("timestamp", entry)
        self.assertTrue(entry["timestamp"].endswith("Z"))
        self.assertNotIn("source_project", entry)
        self.assertNotIn("service_id", entry)

    def test_format_entry_with_exception_chain(self):
        try:
            try:
                raise ValueError("root cause error")
            except ValueError as e:
                raise RuntimeError("wrapper error") from e
        except RuntimeError:
            import sys

            exc_info = sys.exc_info()

        error_data = extract_error_info(exc_info)
        self.assertIsNotNone(error_data)
        assert error_data is not None
        self.assertEqual(error_data["type"], "RuntimeError")
        self.assertEqual(error_data["message"], "wrapper error")
        self.assertGreater(len(error_data["stack"]), 0)

        # Root cause frame should come first
        first_frame = error_data["stack"][0]
        self.assertIn("test_format_entry_with_exception_chain", first_frame["function"])
        self.assertTrue(first_frame["inApp"])

    def test_middleware(self):
        request = MagicMock()
        request.META = {"HTTP_TRACEPARENT": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
        request.headers = {}
        request.method = "GET"
        request.path = "/api/tags"
        request.build_absolute_uri = MagicMock(return_value="http://localhost:8000/api/tags")
        request.user = MagicMock(is_authenticated=False)

        def mock_get_response(req):
            trace = get_trace_context()
            ctx = get_request_context()
            self.assertIsNotNone(trace)
            assert trace is not None
            self.assertEqual(trace["traceId"], "4bf92f3577b34da6a3ce929d0e0e4736")
            self.assertIsNotNone(ctx)
            assert ctx is not None
            self.assertEqual(ctx["http"]["path"], "/api/tags")
            return MagicMock(status_code=200)

        middleware = LogcoreMiddleware(mock_get_response)
        response = middleware(request)
        self.assertEqual(response.status_code, 200)

        # Ensure contexts are cleaned up after request
        self.assertIsNone(get_trace_context())
        self.assertIsNone(get_request_context())

    def test_clamps(self):
        record = logging.LogRecord(
            name="test_logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="a" * 70000,
            args=(),
            exc_info=None,
        )
        record.labels = {f"k{i}": f"v{i}" for i in range(50)}
        entry = format_entry(record)
        self.assertLessEqual(len(entry["message"]), 65536)
        if "labels" in entry:
            self.assertLessEqual(len(entry["labels"]), 32)

    def test_transport_payload_format(self):
        transport = LogcoreTransport()
        transport.config = {
            "url": "https://logcore-dev-352942961463.us-east4.run.app",
            "key": "test-key-123",
            "enabled": True,
            "service": "realworld-django-ninja",
            "env": "dev",
            "min_severity": "ERROR",
        }
        batch = [
            {
                "timestamp": "2026-08-17T18:30:00.000Z",
                "severity": "ERROR",
                "message": "test error",
                "service": "realworld-django-ninja",
                "env": "dev",
                "insert_id": "7c2e1a0b9d8f6e4c2a0b8d6f4e2c0a8b",
            }
        ]

        captured_requests = []

        def mock_urlopen(req, timeout=10.0):
            captured_requests.append(req)
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = False
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = transport._send_http(batch)
            self.assertTrue(res)

        self.assertEqual(len(captured_requests), 1)
        req = captured_requests[0]
        self.assertEqual(req.get_header("X-api-key"), "test-key-123")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(body["entries"][0]["service"], "realworld-django-ninja")
        transport.shutdown()
