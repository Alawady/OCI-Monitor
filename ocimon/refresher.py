"""In-memory cache of the latest result of a source, refreshed on a background thread.

Used for both the resource inventory (Snapshot) and cost data (CostReport).

Scheduled refreshes only run while someone is looking: the page reports
itself with viewer_seen() while its tab is visible, and after `idle_after`
seconds without that the refresher stops calling OCI. When a viewer comes
back to stale data it refreshes at once. Users can also pause scheduled
refreshes; a manual trigger() always runs.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("ocimon.refresher")

IDLE_POLL = 60.0  # how long to sleep when nothing is scheduled; viewer_seen/trigger/resume wake it sooner


class Refresher:
    def __init__(self, source: Callable[[], Any], interval: float, mode: str = "live", name: str = "",
                 idle_after: float = 300) -> None:
        self.source = source
        self.name = name  # log prefix, e.g. "cost"
        self.interval = max(10.0, float(interval))
        self.idle_after = max(0.0, float(idle_after))  # 0 = refresh even with nobody watching
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
        self.paused = False
        self._forced = False
        self._last_attempt: float | None = None  # monotonic time the last refresh attempt ended
        self._last_viewer = time.monotonic()  # startup counts as a view: the browser is opening
        self._was_idle = False

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
            self._last_attempt = time.monotonic()

    @property
    def _prefix(self) -> str:
        return f"{self.name} " if self.name else ""

    # -- scheduling -----------------------------------------------------------
    @property
    def idle(self) -> bool:
        """No visible dashboard tab for `idle_after` seconds."""
        return self.idle_after > 0 and time.monotonic() - self._last_viewer > self.idle_after

    def _stale(self) -> bool:
        return self._last_attempt is None or time.monotonic() - self._last_attempt >= self.interval

    @property
    def next_refresh_at(self) -> float | None:
        """Wall-clock time of the next scheduled refresh, or None when none is scheduled."""
        if self.paused or self.idle or self._last_attempt is None:
            return None
        return time.time() + max(0.0, self.interval - (time.monotonic() - self._last_attempt))

    def viewer_seen(self) -> None:
        """A dashboard tab is visible. Wakes the loop if it was idle (refreshing at once if stale)."""
        was_idle = self.idle
        self._last_viewer = time.monotonic()
        if was_idle:
            self._wake.set()

    def set_paused(self, paused: bool) -> None:
        if paused != self.paused:
            log.info("%sauto-refresh %s", self._prefix, "paused" if paused else "resumed")
        self.paused = paused
        self._wake.set()

    def trigger(self) -> bool:
        """Request an immediate refresh, even while paused or idle. False if one is already running."""
        if self.refreshing:
            return False
        self._forced = True
        self._wake.set()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            idle = self.idle
            if idle != self._was_idle:
                log.info("%s%s", self._prefix, "no one is viewing the dashboard; pausing OCI refreshes until someone does"
                         if idle else "dashboard is being viewed again; resuming refreshes")
                self._was_idle = idle
            forced, self._forced = self._forced, False
            if forced or self._last_attempt is None or (not self.paused and not idle and self._stale()):
                self.refresh_once()
            if self.paused or self.idle:
                timeout = IDLE_POLL
            else:
                timeout = max(0.5, self.interval - (time.monotonic() - (self._last_attempt or 0)))
            self._wake.wait(timeout)

    def start(self) -> None:
        if self._thread is None:
            thread_name = f"ocimon-refresh-{self.name}" if self.name else "ocimon-refresh"
            self._thread = threading.Thread(target=self._loop, name=thread_name, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
