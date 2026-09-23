"""HTTP background transport for logcore structured logging."""

import atexit
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any


class LogcoreTransport:
    """Non-blocking HTTP transport delivering structured logs in batches."""

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
        max_buffer_size: int = 1000,
        batch_size: int = 20,
        max_batch_size: int = 200,
        flush_interval: float = 5.0,
    ) -> None:
        self.url = url or os.getenv("LOGCORE_URL", "https://logcore-dev-352942961463.us-east4.run.app")
        self.endpoint = f"{self.url.rstrip('/')}/v1/logs"
        self.api_key = api_key if api_key is not None else os.getenv("LOGCORE_KEY", "")

        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.getenv("LOGCORE_ENABLED", "true").lower() not in ("false", "0", "no", "off")

        self.max_buffer_size = max_buffer_size
        self.batch_size = batch_size
        self.max_batch_size = max_batch_size
        self.flush_interval = flush_interval

        self._buffer: deque[dict[str, Any]] = deque(maxlen=self.max_buffer_size)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._consecutive_failures = 0
        self._circuit_broken = False

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="logcore-transport-worker")
        self._worker_thread.start()

        atexit.register(self.shutdown)

    def is_enabled(self) -> bool:
        """Return whether transport is enabled."""
        return self.enabled

    def send(self, entry: dict[str, Any]) -> None:
        """Enqueue an entry to be shipped in the next batch."""
        if not self.enabled or self._stop_event.is_set():
            return

        with self._condition:
            if self._circuit_broken:
                return
            self._buffer.append(entry)
            if len(self._buffer) >= self.batch_size:
                self._condition.notify()

    def _worker_loop(self) -> None:
        """Background loop flushing batches periodically or when buffer fills."""
        while not self._stop_event.is_set():
            with self._condition:
                if len(self._buffer) < self.batch_size and not self._stop_event.is_set():
                    self._condition.wait(timeout=self.flush_interval)

                if not self._buffer:
                    continue

                batch: list[dict[str, Any]] = []
                while self._buffer and len(batch) < self.max_batch_size:
                    batch.append(self._buffer.popleft())

            if batch:
                self._deliver_batch(batch)

    def _deliver_batch(self, batch: list[dict[str, Any]]) -> bool:
        """Deliver a batch of entries over HTTP with retries and circuit breaker."""
        payload = {
            "schema_version": 1,
            "entries": batch,
        }
        try:
            payload_bytes = json.dumps(payload).encode("utf-8")
        except Exception as e:
            sys.stderr.write(f"[logcore] Failed to serialize batch: {e}\n")
            return False

        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                req = urllib.request.Request(
                    url=self.endpoint,
                    data=payload_bytes,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10.0) as response:
                    status = response.status
                    if 200 <= status < 300:
                        self._consecutive_failures = 0
                        self._circuit_broken = False
                        return True
            except urllib.error.HTTPError as e:
                # Permanent rejection for 4xx except 429
                if 400 <= e.code < 500 and e.code != 429:
                    try:
                        err_body = e.read().decode("utf-8", errors="replace")
                    except Exception:
                        err_body = str(e)
                    sys.stderr.write(f"[logcore] Permanent rejection ({e.code}): {err_body}\n")
                    self._consecutive_failures = 0
                    return False

                # Retryable 429 or 5xx
                if attempt < max_attempts - 1:
                    time.sleep(2**attempt * 0.25)
                    continue
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < max_attempts - 1:
                    time.sleep(2**attempt * 0.25)
                    continue
                sys.stderr.write(f"[logcore] Network error delivering batch: {e}\n")
            except Exception as e:
                sys.stderr.write(f"[logcore] Unexpected error delivering batch: {e}\n")
                return False

        # All retries failed
        self._consecutive_failures += 1
        if self._consecutive_failures >= 10:
            self._circuit_broken = True
            sys.stderr.write("[logcore] Circuit breaker tripped after 10 consecutive failures. Emptying buffer.\n")
            with self._condition:
                self._buffer.clear()

        return False

    def flush(self) -> None:
        """Synchronously flush all buffered log entries."""
        while True:
            with self._condition:
                if not self._buffer:
                    break
                batch: list[dict[str, Any]] = []
                while self._buffer and len(batch) < self.max_batch_size:
                    batch.append(self._buffer.popleft())

            if batch:
                self._deliver_batch(batch)

    def shutdown(self) -> None:
        """Stop worker thread and flush remaining entries."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
        if self._worker_thread.is_alive() and threading.current_thread() != self._worker_thread:
            self._worker_thread.join(timeout=2.0)
        self.flush()


# Global default transport instance
_default_transport: LogcoreTransport | None = None
_transport_lock = threading.Lock()


def get_default_transport() -> LogcoreTransport:
    """Retrieve or initialize the singleton default transport."""
    global _default_transport
    if _default_transport is None:
        with _transport_lock:
            if _default_transport is None:
                _default_transport = LogcoreTransport()
    transport = _default_transport
    if transport is None:
        transport = LogcoreTransport()
    return transport
