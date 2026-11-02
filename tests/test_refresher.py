"""Scheduling: refresh only while viewed, pause/resume, manual refresh."""

import time

from fastapi.testclient import TestClient

from ocimon.app import create_app
from ocimon.cost import DemoCostSource
from ocimon.demo import DemoSource
from ocimon.refresher import Refresher
from ocimon.settings import Settings


class CountingSource:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self

    def log_summary(self):
        return f"call {self.calls}"


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def fast(refresher, interval=0.1, idle_after=0.0):
    refresher.interval = interval  # the constructor enforces 10s minimum; tests run faster
    refresher.idle_after = idle_after
    return refresher


def test_refreshes_on_schedule_while_viewed():
    src = CountingSource()
    r = fast(Refresher(src, 60), idle_after=0)  # 0 = never idle
    r.start()
    try:
        assert wait_until(lambda: src.calls >= 4)
        assert r.next_refresh_at is not None
    finally:
        r.stop()


def test_goes_idle_without_viewers_and_catches_up_on_return():
    src = CountingSource()
    r = fast(Refresher(src, 60), idle_after=0.3)
    r.start()
    try:
        assert wait_until(lambda: r.idle, timeout=2)
        time.sleep(0.15)  # let any refresh already in flight finish
        settled = src.calls
        time.sleep(0.6)  # several intervals pass with nobody watching
        assert src.calls == settled
        assert r.next_refresh_at is None
        r.viewer_seen()  # the tab becomes visible again; data is stale
        assert wait_until(lambda: src.calls > settled, timeout=1)
        assert not r.idle
    finally:
        r.stop()


def test_pause_stops_schedule_but_manual_refresh_works():
    src = CountingSource()
    r = fast(Refresher(src, 60), idle_after=0)
    r.start()
    try:
        assert wait_until(lambda: src.calls >= 1)
        r.set_paused(True)
        time.sleep(0.15)
        settled = src.calls
        time.sleep(0.5)
        assert src.calls == settled and r.next_refresh_at is None
        assert r.trigger()  # "Refresh now" still works while paused
        assert wait_until(lambda: src.calls == settled + 1)
        time.sleep(0.4)
        assert src.calls == settled + 1  # and doesn't restart the schedule
        r.set_paused(False)
        assert wait_until(lambda: src.calls >= settled + 3)
    finally:
        r.stop()


def test_first_refresh_runs_even_when_paused_or_idle():
    src = CountingSource()
    r = fast(Refresher(src, 60), idle_after=0.01)
    r.set_paused(True)
    time.sleep(0.05)  # already idle before starting
    r.start()
    try:
        assert wait_until(lambda: src.calls == 1)  # the page needs something to show
    finally:
        r.stop()


def test_api_viewer_heartbeat_and_pause_endpoint():
    inv = Refresher(DemoSource(delay=0), 120, mode="demo")
    inv.refresh_once()
    cost = Refresher(DemoCostSource(lambda: inv.snapshot, delay=0), 3600, mode="demo", name="cost")
    cost.refresh_once()
    c = TestClient(create_app(Settings(), inv, {"kind": "demo"}, cost), base_url="http://127.0.0.1:8080")

    for r in (inv, cost):
        r._last_viewer -= 10_000  # nobody has looked for hours
    assert c.get("/api/health").json()["auto_refresh"]["idle"] is True  # a plain request isn't a viewer
    d = c.get("/api/health", params={"viewer": 1}).json()
    assert d["auto_refresh"] == {"paused": False, "idle": False, "idle_after": 300.0}
    assert not cost.idle  # cost refreshes follow the same viewer

    assert c.post("/api/auto-refresh", params={"enabled": "false"}).status_code == 403  # needs X-OCIMON
    r = c.post("/api/auto-refresh", params={"enabled": "false"}, headers={"X-OCIMON": "1"})
    assert r.json() == {"paused": True} and inv.paused and cost.paused
    d = c.get("/api/health").json()
    assert d["auto_refresh"]["paused"] and d["next_refresh_at"] is None and d["cost"]["paused"]
    c.post("/api/auto-refresh", params={"enabled": "true"}, headers={"X-OCIMON": "1"})
    assert not inv.paused and not cost.paused
