"""In-memory cache of the latest result of a source, refreshed on a background thread.

Used for both the resource inventory (Snapshot) and cost data (CostReport).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("ocimon.refresher")


class Refresher:
    def __init__(self, source: Callable[[], Any], interval: float, mode: str = "live", name: str = "") -> None:
        self.source = source
        self.name = name  # log prefix, e.g. "cost"

        self.interval = max(10.0, float(interval))
        self.mode = mode
        self._snapshot: Any = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        if hasattr(source, "shutdown_event"):
            source.shutdown_event = self._stop  # lets stop() cancel an in-flight refresh
        self._thread: threading.Thread | None = None
        self.refreshing = False
        self.refresh_count = 0
        self.last_failure: str | None = None
        self.next_refresh_at: float | None = None

    @property
    def snapshot(self) -> Any:
        with self._lock:
            return self._snapshot

    def refresh_once(self) -> Any:
        with self._lock:
            if self.refreshing:
                return self._snapshot
            self.refreshing = True
        try:
            snap = self.source()
        except Exception as exc:
            if self._stop.is_set():
                # Shutting down mid-refresh (Ctrl+C, server exit): thread pools refuse new work. Not an error.
                return self.snapshot
            # Keep serving the previous snapshot; surface the failure in /api/health.
            log.exception("%srefresh failed", self._prefix)
            self.last_failure = f"{type(exc).__name__}: {exc}"[:500]
            return self.snapshot
        else:
            with self._lock:
                self._snapshot = snap
                self.refresh_count += 1
            self.last_failure = None
            log.info("%srefresh #%d: %s", self._prefix, self.refresh_count, snap.log_summary())
            return snap
        finally:
            with self._lock:
                self.refreshing = False

    @property
    def _prefix(self) -> str:
        return f"{self.name} " if self.name else ""

    def trigger(self) -> bool:
        """Request an immediate refresh. Returns False if one is already running."""
        if self.refreshing:
            return False
        self._wake.set()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self.refresh_once()
            self.next_refresh_at = time.time() + self.interval
            self._wake.wait(self.interval)

    def start(self) -> None:
        if self._thread is None:
            thread_name = f"ocimon-refresh-{self.name}" if self.name else "ocimon-refresh"
            self._thread = threading.Thread(target=self._loop, name=thread_name, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
