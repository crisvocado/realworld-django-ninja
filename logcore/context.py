from __future__ import annotations

import contextvars
from typing import Any

current_trace: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar[dict[str, Any] | None](
    "logcore_current_trace", default=None
)
