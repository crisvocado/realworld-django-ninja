"""Logcore structured logging integration for Django."""

import sys
from pathlib import Path

# Ensure local venv site-packages is in sys.path if running under system python
try:
    import django  # noqa: F401
except ImportError:
    base_dir = Path(__file__).resolve().parent.parent
    venv_lib = base_dir / ".venv" / "lib"
    if venv_lib.exists():
        for py_dir in venv_lib.glob("python*"):
            sp = py_dir / "site-packages"
            if sp.exists() and str(sp) not in sys.path:
                sys.path.insert(0, str(sp))

from logcore.context import (
    get_current_context,
    get_current_trace,
    parse_traceparent,
    reset_context,
    reset_trace,
    set_context,
    set_trace,
)
from logcore.formatter import format_entry, format_exception
from logcore.handler import LogcoreHandler
from logcore.middleware import LogcoreMiddleware
from logcore.transport import LogcoreTransport, get_default_transport

__all__ = [
    "LogcoreHandler",
    "LogcoreMiddleware",
    "LogcoreTransport",
    "format_entry",
    "format_exception",
    "get_current_context",
    "get_current_trace",
    "get_default_transport",
    "parse_traceparent",
    "reset_context",
    "reset_trace",
    "set_context",
    "set_trace",
]
