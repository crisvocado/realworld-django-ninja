"""Best-effort HTTP logcore handler for standard-library logging."""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any


def _enabled() -> bool:
    return os.getenv("LOGCORE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def _frames(exc: BaseException | None) -> list[dict[str, Any]]:
    if exc is None or exc.__traceback__ is None:
        return []
    frames = []
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        frames.append(
            {
                "function": frame.name,
                "file": frame.filename,
                "line": frame.lineno,
                "inApp": "site-packages" not in frame.filename,
            }
        )
    return frames[:50]


class LogcoreHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.url = os.getenv("LOGCORE_URL", "").rstrip("/") + "/v1/logs"
        self.key = os.getenv("LOGCORE_KEY", "")
        self.service = os.getenv("LOGCORE_SERVICE", "realworld-django-ninja")
        self.env = os.getenv("LOGCORE_ENV", "dev")
        self._items: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        if not _enabled() or not self.url or not self.key or record.levelno < logging.ERROR:
            return
        try:
            message = self.format(record)[:65536]
            timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            entry: dict[str, Any] = {
                "timestamp": timestamp,
                "severity": "CRITICAL" if record.levelno >= 50 else "ERROR",
                "message": message,
                "service": self.service,
                "env": self.env,
            }
            context = {"logger": record.name}
            identity = f"{timestamp}|{self.service}|{entry['severity']}|{message}|{json.dumps(context, sort_keys=True)}"
            entry["insert_id"] = hashlib.sha256(identity.encode()).hexdigest()[:32]
            if record.exc_info:
                exc = record.exc_info[1] if record.exc_info[1] is not None else None
                entry["error"] = {"type": type(exc).__name__, "message": str(exc)[:65536], "stack": _frames(exc)}
            entry["context"] = context
            try:
                self._items.put_nowait(entry)
            except queue.Full:
                self._items.get_nowait()
                self._items.put_nowait(entry)
        except Exception:
            return

    def _run(self) -> None:
        while not self._stop.wait(5):
            self.flush()

    def flush(self) -> None:
        entries = []
        while len(entries) < 200:
            try:
                entries.append(self._items.get_nowait())
            except queue.Empty:
                break
        if not entries:
            return
        try:
            request = urllib.request.Request(
                self.url,
                data=json.dumps({"schema_version": 1, "entries": entries}).encode(),
                headers={"Content-Type": "application/json", "x-api-key": self.key},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2):
                pass
        except Exception as exc:
            print(f"logcore delivery failed: {exc}", file=sys.stderr)

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._stop.set()
            super().close()


def install() -> None:
    root = logging.getLogger()
    if not any(isinstance(handler, LogcoreHandler) for handler in root.handlers):
        root.addHandler(LogcoreHandler())


atexit.register(
    lambda: [handler.close() for handler in logging.getLogger().handlers if isinstance(handler, LogcoreHandler)]
)
