"""Probe entry point for logcore structured logging verification."""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Ensure local venv site-packages is in sys.path if running under system python
try:
    import django
except ImportError:
    base_dir = Path(__file__).resolve().parent.parent
    venv_lib = base_dir / ".venv" / "lib"
    if venv_lib.exists():
        for py_dir in venv_lib.glob("python*"):
            sp = py_dir / "site-packages"
            if sp.exists() and str(sp) not in sys.path:
                sys.path.insert(0, str(sp))
    import django


def main() -> None:
    """Initialize Django and emit a single test ERROR log for validation."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DEBUG", "True")
    django.setup()

    from logcore.handler import LogcoreHandler
    from logcore.transport import get_default_transport

    transport = get_default_transport()

    # Stub at the send boundary: capture entry and print JSON to stdout
    def stubbed_send(entry: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(entry) + "\n")
        sys.stdout.flush()

    transport.send = stubbed_send  # type: ignore[method-assign]

    # Also hook any existing handlers on root or logger hierarchy
    for handler in logging.root.handlers:
        if isinstance(handler, LogcoreHandler):
            handler.transport.send = stubbed_send  # type: ignore[method-assign]

    logger = logging.getLogger("logcore.probe")
    logger.error("Logcore probe test error")


if __name__ == "__main__":
    main()
