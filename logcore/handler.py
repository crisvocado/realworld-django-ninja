"""Logging handler forwarding ERROR and higher log records to Logcore."""

import logging
import sys

from logcore.client import get_client
from logcore.config import get_config
from logcore.formatter import format_record


class LogcoreHandler(logging.Handler):
    """Logging handler that forwards ERROR and above records to Logcore."""

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self.config = get_config()
        self.client = get_client()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Self-exclusion: never process logs from logcore internal modules
            if record.name.startswith("logcore"):
                return

            # Check severity floor
            if record.levelno < self.config.min_severity_level:
                return

            entry = format_record(record, config=self.config)
            self.client.enqueue(entry)
        except Exception:
            # Logging must NEVER break the application
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        # Avoid recursive logging by writing directly to sys.stderr
        if sys.stderr:
            try:
                sys.stderr.write(f"[logcore] Error processing log record: {record.getMessage()}\n")
                sys.stderr.flush()
            except Exception:
                pass

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.client.shutdown()
        except Exception:
            pass
        super().close()
