"""Entry point: `python -m ocimon`."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser

from . import __version__
from .settings import Settings, parse_args

log = logging.getLogger("ocimon")


def _is_wsl() -> bool:
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def open_browser(url: str) -> None:
    # Under WSL the Linux side usually has no browser; hand the URL to Windows.
    if _is_wsl() and shutil.which("explorer.exe"):
        subprocess.Popen(["explorer.exe", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        webbrowser.open(url, new=2)
    except Exception:
        pass


def build_source(settings: Settings):
    """Return (snapshot source callable, mode, non-secret auth description)."""
    if settings.demo:
        from .demo import DemoSource
        return DemoSource(), "demo", {"kind": "demo", "profile": None}

    from .auth import AuthError, load_auth
    from .engine import LiveSource
    try:
        auth = load_auth(settings.config_file, settings.profile, settings.auth)
    except AuthError as exc:
        sys.exit(f"ocimon: {exc}")
    log.info("auth=%s profile=%s region=%s", auth.kind, auth.profile, auth.region)
    return (LiveSource(auth, settings.regions, settings.compartment, settings.workers),
            "live", auth.describe)


def main(argv: list[str] | None = None) -> None:
    settings = parse_args(argv)
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("oci").setLevel(logging.WARNING)

    if not settings.is_local:
        print(f"WARNING: binding to {settings.host} exposes your OCI inventory on the network.", file=sys.stderr)
        if not settings.token:
            sys.exit("ocimon: a non-local --host requires an access token (--token or OCIMON_TOKEN).")

    source, mode, auth_info = build_source(settings)

    import uvicorn

    from .app import create_app
    from .refresher import Refresher

    refresher = Refresher(source, settings.refresh, mode=mode)
    app = create_app(settings, refresher, auth_info)
    refresher.start()

    shown_host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    if ":" in shown_host:
        shown_host = f"[{shown_host}]"
    url = f"http://{shown_host}:{settings.port}{settings.base_path}/"
    print(f"OCI Monitor {__version__} ({mode}) on {url}", flush=True)
    if settings.open_browser:
        browse = url + (f"?token={settings.token}" if settings.token else "")
        threading.Timer(1.0, open_browser, args=(browse,)).start()

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=settings.log_level == "debug",
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
