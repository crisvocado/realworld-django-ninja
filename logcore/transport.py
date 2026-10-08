from __future__ import annotations

import atexit
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

MAX_ENTRIES_PER_REQUEST = 200
BATCH_SIZE = 20
FLUSH_INTERVAL = 5.0
BUFFER_CAP = 1000
MAX_CONSECUTIVE_FAILURES = 10
MAX_RETRIES = 3


class HttpTransport:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        raw_url = url or os.getenv("LOGCORE_URL", "https://logcore-dev-352942961463.us-east4.run.app")
        self.endpoint = f"{raw_url.rstrip('/')}/v1/logs"
        self.api_key = api_key or os.getenv("LOGCORE_KEY", "")

        if enabled is not None:
            self.enabled = enabled
        else:
            raw_enabled = os.getenv("LOGCORE_ENABLED", "").lower().strip()
            if raw_enabled in ("false", "0", "no"):
                self.enabled = False
            else:
                self.enabled = True

        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=BUFFER_CAP)
        self._consecutive_failures = 0
        self._capture_sink: Callable[[dict[str, Any]], None] | None = None
        self._lock = threading.Lock()
        self._running = True

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="LogcoreTransportWorker")
        self._worker_thread.start()

        atexit.register(self.flush_and_stop)

    def set_capture_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        with self._lock:
            self._capture_sink = sink

    def enqueue(self, entry: dict[str, Any]) -> None:
        if not self.enabled and self._capture_sink is None:
            return

        with self._lock:
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Circuit breaker open: drop buffer
                self._drain_queue_internal()
                return

        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            # Buffer full: drop oldest
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(entry)
            except queue.Full:
                pass

    def _drain_queue_internal(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    items.append(item)
            except queue.Empty:
                break
        return items

    def _send_batch(self, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return True

        envelope = {
            "schema_version": 1,
            "entries": batch,
        }

        with self._lock:
            capture_sink = self._capture_sink

        if capture_sink is not None:
            try:
                capture_sink(envelope)
                self._consecutive_failures = 0
                return True
            except Exception as e:
                sys.stderr.write(f"[logcore] Capture sink error: {e}\n")
                sys.stderr.flush()
                return False

        if not self.enabled or not self.api_key:
            return True

        payload_bytes = json.dumps(envelope).encode("utf-8")

        for attempt in range(MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    url=self.endpoint,
                    data=payload_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self.api_key,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status in (200, 201, 202, 204):
                        with self._lock:
                            self._consecutive_failures = 0
                        return True
            except urllib.error.HTTPError as e:
                status = e.code
                reason = e.read().decode("utf-8", errors="replace")
                if status == 429 or status >= 500:
                    # Retryable
                    if attempt < MAX_RETRIES:
                        time.sleep((2**attempt) * 0.25)
                        continue
                    with self._lock:
                        self._consecutive_failures += 1
                    return False
                else:
                    # Permanent rejection (e.g. 400, 401, 403, 422)
                    sys.stderr.write(f"[logcore] Permanent rejection ({status}): {reason}\n")
                    sys.stderr.flush()
                    with self._lock:
                        self._consecutive_failures += 1
                    return False
            except Exception as e:
                # Network error -> retry
                if attempt < MAX_RETRIES:
                    time.sleep((2**attempt) * 0.25)
                    continue
                sys.stderr.write(f"[logcore] Network failure during send: {e}\n")
                sys.stderr.flush()
                with self._lock:
                    self._consecutive_failures += 1
                return False

        return False

    def _worker_loop(self) -> None:
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        while self._running:
            timeout = max(0.1, FLUSH_INTERVAL - (time.monotonic() - last_flush))
            try:
                item = self._queue.get(timeout=timeout)
                if item is not None:
                    batch.append(item)
                    if len(batch) >= BATCH_SIZE or len(batch) >= MAX_ENTRIES_PER_REQUEST:
                        self._send_batch(batch)
                        batch = []
                        last_flush = time.monotonic()
            except queue.Empty:
                if batch:
                    self._send_batch(batch)
                    batch = []
                    last_flush = time.monotonic()

        # Flush any remaining items when shutting down
        if batch:
            self._send_batch(batch)

    def flush(self) -> None:
        """Drains the queue synchronously and sends all buffered items."""
        items = self._drain_queue_internal()
        for i in range(0, len(items), MAX_ENTRIES_PER_REQUEST):
            chunk = items[i : i + MAX_ENTRIES_PER_REQUEST]
            self._send_batch(chunk)

    def flush_and_stop(self) -> None:
        self._running = False
        self.flush()


_global_transport: HttpTransport | None = None
_transport_lock = threading.Lock()


def get_transport() -> HttpTransport:
    global _global_transport
    if _global_transport is None:
        with _transport_lock:
            if _global_transport is None:
                _global_transport = HttpTransport()
    res = _global_transport
    if res is None:
        res = HttpTransport()
    return res
