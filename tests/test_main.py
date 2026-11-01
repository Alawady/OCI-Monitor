"""Startup guards in the CLI entry point."""

import socket

import pytest

from ocimon.__main__ import main, port_in_use
from ocimon.refresher import Refresher


def test_port_in_use_detects_listener():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert port_in_use("127.0.0.1", port)
    assert not port_in_use("127.0.0.1", port)


def test_main_exits_cleanly_when_port_taken(capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        with pytest.raises(SystemExit) as exc:
            main(["--demo", "--no-browser", "--port", str(port)])
    assert "already in use" in str(exc.value.code)


def test_refresh_failure_during_shutdown_is_quiet(caplog):
    def source():
        refresher.stop()  # shutdown begins while the refresh is running
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    refresher = Refresher(source, 60)
    assert refresher.refresh_once() is None
    assert refresher.last_failure is None
    assert "refresh failed" not in caplog.text
