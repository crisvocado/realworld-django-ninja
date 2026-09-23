"""Standard library logging.Handler implementation for logcore."""

import logging
import os
from pathlib import Path
from typing import Any

from logcore.formatter import format_entry
from logcore.transport import LogcoreTransport, get_default_transport

SEVERITY_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
}


class LogcoreHandler(logging.Handler):
    """Logging handler that formats and ships ERROR+ log records to logcore."""

    def __init__(
        self,
        level: int | str = logging.ERROR,
        url: str | None = None,
        api_key: str | None = None,
        transport: LogcoreTransport | None = None,
        base_dir: Path | str | None = None,
    ) -> None:
        super().__init__(level=level)

        if transport is not None:
            self.transport = transport
        elif url is not None or api_key is not None:
            self.transport = LogcoreTransport(url=url, api_key=api_key)
        else:
            self.transport = get_default_transport()

        if base_dir:
            self.base_dir = Path(base_dir).resolve()
        else:
            self.base_dir = Path(__file__).resolve().parent.parent

        min_sev_name = os.getenv("LOGCORE_MIN_SEVERITY", "ERROR").upper()
        self.min_level = SEVERITY_LEVELS.get(min_sev_name, logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        """Process and transmit a log record."""
        try:
            if not self.transport.is_enabled():
                return
            if record.levelno < self.min_level:
                return

            entry: dict[str, Any] = format_entry(record, base_dir=self.base_dir)
            self.transport.send(entry)
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        """Flush pending entries in the transport."""
        try:
            self.transport.flush()
        except Exception:
            pass

    def close(self) -> None:
        """Close handler and shut down transport."""
        try:
            self.transport.shutdown()
        except Exception:
            pass
        super().close()
