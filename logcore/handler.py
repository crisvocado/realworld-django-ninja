from __future__ import annotations

import logging
import os
from typing import Any

from logcore.formatter import LogcoreFormatter
from logcore.transport import get_transport

LEVEL_NAMES: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def get_min_severity_level() -> int:
    env_val = os.getenv("LOGCORE_MIN_SEVERITY", "ERROR").upper().strip()
    return LEVEL_NAMES.get(env_val) or logging.ERROR


class LogcoreHandler(logging.Handler):
    def __init__(self, level: int | str = logging.ERROR) -> None:
        if isinstance(level, str):
            level_num = LEVEL_NAMES.get(level.upper(), logging.ERROR)
        else:
            level_num = int(level)
        super().__init__(level_num)
        self.logcore_formatter = LogcoreFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < get_min_severity_level():
                return
            entry: dict[str, Any] = self.logcore_formatter.format_record(record)
            transport = get_transport()
            transport.enqueue(entry)
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        transport = get_transport()
        transport.flush()
