"""Command-line flags and environment variables."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from . import __version__
from .auth import AUTH_KINDS

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8080
    base_path: str = ""
    config_file: str | None = None
    profile: str | None = None
    auth: str = "auto"
    regions: str | None = None
    compartment: str | None = None
    refresh: int = 120
    cost: bool = True
    cost_refresh: int = 3600
    demo: bool = False
    token: str | None = None
    open_browser: bool = True
    workers: int = 16
    log_level: str = "info"

    @property
    def is_local(self) -> bool:
        return self.host in LOCAL_HOSTS


def normalize_base_path(value: str | None) -> str:
    """'' or '/ocimon' (leading slash, no trailing slash)."""
    value = (value or "").strip().strip("/")
    return f"/{value}" if value else ""


def parse_args(argv: list[str] | None = None) -> Settings:
    env = os.environ
    p = argparse.ArgumentParser(
        prog="python -m ocimon",
        description="Read-only OCI monitoring dashboard. Uses your OCI CLI config; never modifies resources.",
    )
    p.add_argument("--host", default=env.get("OCIMON_HOST", "127.0.0.1"),
                   help="bind address (default 127.0.0.1). Non-local hosts require --token.")
    p.add_argument("--port", type=int, default=int(env.get("OCIMON_PORT", "8080")), help="port (default 8080)")
    p.add_argument("--base-path", default=env.get("OCIMON_BASE_PATH", ""),
                   help="URL prefix when served behind a reverse proxy, e.g. /ocimon")
    p.add_argument("--config-file", default=env.get("OCI_CLI_CONFIG_FILE"),
                   help="OCI config file (default ~/.oci/config)")
    p.add_argument("--profile", default=env.get("OCI_CLI_PROFILE"), help="config profile (default DEFAULT)")
    p.add_argument("--auth", choices=AUTH_KINDS, default=env.get("OCI_CLI_AUTH", "auto").replace("-", "_"),
                   help="auth type; 'auto' uses security_token_file if the profile has one, else the API key")
    p.add_argument("--regions", default=env.get("OCIMON_REGIONS"),
                   help="comma-separated regions, or 'all' subscribed regions (default: the profile's region)")
    p.add_argument("--compartment", default=env.get("OCIMON_COMPARTMENT"),
                   help="root compartment OCID to monitor (default: the tenancy)")
    p.add_argument("--refresh", type=int, default=int(env.get("OCIMON_REFRESH", "120")),
                   help="background refresh interval in seconds (default 120, minimum 10)")
    p.add_argument("--cost-refresh", type=int, default=int(env.get("OCIMON_COST_REFRESH", "3600")),
                   help="cost data refresh interval in seconds (default 3600, minimum 300)")
    p.add_argument("--no-cost", action="store_true", help="don't fetch cost data (hides the Cost tab)")
    p.add_argument("--demo", action="store_true", help="serve realistic fake data; no OCI access needed")
    p.add_argument("--token", default=None, help="access token required on every request (or OCIMON_TOKEN)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser on start")
    p.add_argument("--workers", type=int, default=int(env.get("OCIMON_WORKERS", "16")),
                   help="parallel OCI API calls (default 16)")
    p.add_argument("--log-level", default=env.get("OCIMON_LOG_LEVEL", "info"),
                   choices=["debug", "info", "warning", "error"])
    p.add_argument("--version", action="version", version=f"ocimon {__version__}")
    a = p.parse_args(argv)
    return Settings(
        host=a.host,
        port=a.port,
        base_path=normalize_base_path(a.base_path),
        config_file=a.config_file,
        profile=a.profile,
        auth=a.auth,
        regions=a.regions,
        compartment=a.compartment,
        refresh=max(10, a.refresh),
        cost=not a.no_cost,
        cost_refresh=max(300, a.cost_refresh),
        demo=a.demo,
        # Prefer the environment so the token doesn't show up in `ps` output.
        token=a.token or env.get("OCIMON_TOKEN") or None,
        open_browser=not a.no_browser,
        workers=max(2, a.workers),
        log_level=a.log_level,
    )
