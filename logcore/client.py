"""HTTP transport client for Logcore delivery with batching, retries, and circuit breaker."""

import atexit
import collections
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from logcore.config import LogcoreConfig, get_config


class LogcoreClient:
    def __init__(self, config: LogcoreConfig | None = None) -> None:
        self.config = config or get_config()
        self._buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=1000)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._consecutive_failures = 0
        self._worker_thread: threading.Thread | None = None
        self._started = False
        self._custom_sender: Callable[[list[dict[str, Any]]], None] | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="logcore-delivery-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def enqueue(self, entry: dict[str, Any]) -> None:
        if not self._started and not self._stop_event.is_set():
            self.start()

        with self._condition:
            if self._consecutive_failures >= 10:
                # Circuit breaker open: drop entry
                return
            self._buffer.append(entry)
            if len(self._buffer) >= 20:
                self._condition.notify_all()

    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._buffer:
                    break
                batch = self._drain_batch(200)
            if batch:
                self._send_batch(batch)
            else:
                break

    def shutdown(self, timeout: float = 3.0) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            self._stop_event.set()

        with self._condition:
            self._condition.notify_all()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

        # Final synchronous flush of remaining items
        self.flush(timeout=timeout)

    def _drain_batch(self, max_items: int = 200) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        while self._buffer and len(batch) < max_items:
            batch.append(self._buffer.popleft())
        return batch

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._condition:
                while not self._stop_event.is_set() and len(self._buffer) < 20:
                    self._condition.wait(timeout=5.0)
                    break
                if self._stop_event.is_set() and not self._buffer:
                    break
                batch = self._drain_batch(200)

            if batch:
                self._send_batch(batch)

    def _send_batch(self, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return True

        if self._custom_sender is not None:
            try:
                self._custom_sender(batch)
                self._consecutive_failures = 0
                return True
            except Exception as e:
                self._consecutive_failures += 1
                sys.stderr.write(f"[logcore] Custom sender error: {e}\n")
                sys.stderr.flush()
                return False

        if not self.config.is_active():
            # Inactive (no API key or disabled) — drop silently
            return True

        payload = {
            "schema_version": 1,
            "entries": batch,
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        url = f"{self.config.url}/v1/logs"

        for attempt in range(4):  # Initial try + up to 3 retries
            try:
                req = urllib.request.Request(
                    url=url,
                    data=body_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self.config.api_key,
                        "User-Agent": "logcore-python/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10.0) as response:
                    status = response.status
                    if 200 <= status < 300:
                        self._consecutive_failures = 0
                        return True
            except urllib.error.HTTPError as e:
                status = e.code
                if status == 429 or status >= 500:
                    # Retryable server error or rate limit
                    if attempt < 3:
                        time.sleep((2**attempt) * 0.25)
                        continue
                    else:
                        self._handle_failure()
                        return False
                else:
                    # Permanent 4xx error (e.g., 400, 401, 403, 422)
                    try:
                        err_body = e.read().decode("utf-8", errors="replace")
                    except Exception:
                        err_body = str(e)
                    sys.stderr.write(f"[logcore] Permanent delivery failure ({status}): {err_body}\n")
                    sys.stderr.flush()
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 10:
                        with self._lock:
                            self._buffer.clear()
                    return False
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                # Network or timeout error (retryable)
                if attempt < 3:
                    time.sleep((2**attempt) * 0.25)
                    continue
                else:
                    self._handle_failure(str(e))
                    return False
            except Exception as e:
                self._handle_failure(str(e))
                return False

        return False

    def _handle_failure(self, reason: str = "") -> None:
        self._consecutive_failures += 1
        if reason:
            sys.stderr.write(f"[logcore] Delivery failed after retries: {reason}\n")
            sys.stderr.flush()
        if self._consecutive_failures >= 10:
            with self._lock:
                self._buffer.clear()


_client_instance: LogcoreClient | None = None
_client_lock = threading.Lock()


def get_client() -> LogcoreClient:
    global _client_instance
    instance = _client_instance
    if instance is None:
        with _client_lock:
            instance = _client_instance
            if instance is None:
                instance = LogcoreClient()
                atexit.register(instance.shutdown)
                _client_instance = instance
    return instance
