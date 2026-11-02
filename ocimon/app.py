"""FastAPI app: JSON API plus the static single-page UI."""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .collectors import resource_types
from .cost import cost_view
from .metrics import THRESHOLDS
from .filters import ResourceQuery, compartment_tree, filter_resources, resource_view, summarize
from .models import CATEGORIES, isoformat
from .refresher import Refresher
from .settings import LOCAL_HOSTS, Settings
from .snapshot import empty_snapshot

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_COOKIE = "ocimon_token"

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _hostname(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header[1:].split("]", 1)[0]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def _filters(compartment: str | None, region: str | None, type_: str | None, state: str | None,
             q: str | None, problems: bool, include_terminated: bool, category: str | None,
             subtree: bool) -> ResourceQuery:
    return ResourceQuery.from_params(compartment=compartment, subtree=subtree, type=type_, category=category,
                                     state=state, region=region, q=q, problems=problems,
                                     include_terminated=include_terminated)


def create_app(settings: Settings, refresher: Refresher,
               auth_info: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
               cost_refresher: Refresher | None = None) -> FastAPI:
    api = FastAPI(title="OCI Monitor", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    def snap():
        return refresher.snapshot or empty_snapshot(refresher.mode)

    # Query parameters shared by summary, compartments and resources.
    def query_params(compartment: str | None = None, region: str | None = None,
                     type_: str | None = Query(None, alias="type"), state: str | None = None,
                     q: str | None = None, problems: bool = False, include_terminated: bool = False,
                     category: str | None = None, subtree: bool = True) -> ResourceQuery:
        return _filters(compartment, region, type_, state, q, problems, include_terminated, category, subtree)

    @api.get("/api/summary")
    def summary(query: ResourceQuery = Depends(query_params)):
        s = snap()
        return {**summarize(filter_resources(s, query)), "last_refresh": s.finished_at or None}

    @api.get("/api/compartments")
    def compartments(query: ResourceQuery = Depends(query_params)):
        s = snap()
        query.compartment = None  # the tree always shows everything; counts follow the other filters
        return {"root_id": s.root_id or None, "tree": compartment_tree(s, filter_resources(s, query))}

    @api.get("/api/resources")
    def resources(query: ResourceQuery = Depends(query_params)):
        s = snap()
        types = resource_types()
        order = {c.key: i for i, c in enumerate(CATEGORIES)}
        type_order = {k: i for i, k in enumerate(types)}
        items = filter_resources(s, query)
        items.sort(key=lambda r: (order.get(types[r.type].category if r.type in types else "", 99),
                                  type_order.get(r.type, 99), r.name.lower()))
        report = cost_report()
        return {
            "items": [resource_view(s, r, report) for r in items],
            "currency": report.currency if report else None,
            "count": len(items),
            "total": len(s.resources),
            "last_refresh": s.finished_at or None,
        }

    @api.get("/api/health")
    def health():
        s = refresher.snapshot
        errors = [e.__dict__ | {"compartment_name": s.compartment_name(e.compartment_id),
                                "compartment_path": s.paths.get(e.compartment_id or "")}
                  for e in (s.errors if s else [])]
        if s is None:
            status = "error" if refresher.last_failure else "loading"
        elif refresher.last_failure:
            status = "stale"
        else:
            status = "degraded" if errors else "ok"
        return {
            "status": status,
            "mode": refresher.mode,
            "version": __version__,
            "refreshing": refresher.refreshing,
            "refresh_count": refresher.refresh_count,
            "refresh_interval": refresher.interval,
            "last_refresh": s.finished_at if s else None,
            "last_refresh_iso": isoformat(_ts(s.finished_at)) if s else None,
            "duration_s": round(s.duration, 2) if s else None,
            "next_refresh_at": refresher.next_refresh_at,
            "last_failure": refresher.last_failure,
            "resource_count": len(s.resources) if s else 0,
            "regions": s.regions if s else [],
            "tenancy_name": s.tenancy_name if s else None,
            "stats": s.stats if s else {},
            "auth": auth_info() if callable(auth_info) else auth_info,
            "errors": errors,
            "cost": cost_status(),
        }

    def cost_report():
        report = cost_refresher.snapshot if cost_refresher else None
        return report if report is not None and not report.error else None

    def cost_status() -> dict[str, Any]:
        if cost_refresher is None:
            return {"status": "disabled"}
        report = cost_refresher.snapshot
        if report is None:
            status = "error" if cost_refresher.last_failure else "loading"
        else:
            status = "error" if report.error else ("stale" if cost_refresher.last_failure else "ok")
        return {
            "status": status,
            "refreshing": cost_refresher.refreshing,
            "refresh_interval": cost_refresher.interval,
            "last_refresh": report.finished_at if report else None,
            "next_refresh_at": cost_refresher.next_refresh_at,
            "duration_s": round(report.elapsed, 2) if report else None,
            "error": report.error if report else cost_refresher.last_failure,
            "error_kind": report.error_kind if report else ("error" if cost_refresher.last_failure else None),
            "last_failure": cost_refresher.last_failure,
        }

    @api.get("/api/cost")
    def cost(days: int = 7, compartment: str | None = None, region: str | None = None):
        status = cost_status()
        report = cost_refresher.snapshot if cost_refresher else None
        if report is None or report.error:
            return {"status": status["status"], "meta": status}
        s = refresher.snapshot
        view = cost_view(report, s, days, compartment if s and compartment in s.compartments else None, region)
        return {**view, "meta": status}

    @api.post("/api/refresh", status_code=202)
    def refresh(target: str = "resources"):
        if target == "cost":
            if cost_refresher is None:
                return JSONResponse({"detail": "Cost data is disabled"}, status_code=409)
            accepted = cost_refresher.trigger()
        else:
            accepted = refresher.trigger()
        return {"accepted": accepted, "refreshing": True, "target": target}

    @api.get("/api/meta")
    def meta():
        s = refresher.snapshot
        return {
            "version": __version__,
            "mode": refresher.mode,
            "regions": s.regions if s else [],
            "tenancy_name": s.tenancy_name if s else None,
            "cost_enabled": cost_refresher is not None,
            "thresholds": {k: {"label": t.label, "warn": t.warn, "crit": t.crit} for k, t in THRESHOLDS.items()},
            "categories": [c.__dict__ for c in CATEGORIES],
            "types": [{"key": t.key, "label": t.label, "category": t.category} for t in resource_types().values()],
        }

    @api.get("/", include_in_schema=False)
    def index():
        # Version the asset URLs by file time, so browsers that cached an older copy fetch the new one.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for asset in ("app.js", "styles.css"):
            version = (STATIC_DIR / asset).stat().st_mtime_ns
            html = html.replace(f'static/{asset}"', f'static/{asset}?v={version}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    if settings.base_path:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.mount(settings.base_path, api)

        @app.get(settings.base_path, include_in_schema=False)
        def base_redirect():
            return RedirectResponse(settings.base_path + "/")
    else:
        app = api

    token = settings.token
    cookie_path = settings.base_path + "/" if settings.base_path else "/"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if token:
            supplied = request.query_params.get("token")
            via_query = supplied is not None
            if not via_query:
                auth = request.headers.get("authorization", "")
                supplied = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(TOKEN_COOKIE)
            if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
                if "/api/" in request.url.path:
                    return JSONResponse({"detail": "Access token required"}, status_code=401)
                return PlainTextResponse("Access token required. Open this URL with ?token=<your token>.",
                                         status_code=401)
            if via_query and request.method == "GET":
                # Swap the token in the URL for an HttpOnly cookie, so it isn't kept in history.
                response = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
                response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="strict", path=cookie_path,
                                    secure=request.url.scheme == "https")
                return response
        elif _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
            # Without a token, only answer to localhost names (blocks DNS rebinding).
            return PlainTextResponse("Host not allowed. Use --token when serving on another name.",
                                     status_code=403)
        if request.method == "POST" and request.headers.get("x-ocimon") != "1":
            # Custom header forces a CORS preflight, so other sites can't POST here.
            return JSONResponse({"detail": "Missing X-OCIMON header"}, status_code=403)

        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if "/api/" in request.url.path:
            response.headers["Cache-Control"] = "no-store"
        elif "/static/" in request.url.path:
            # Revalidate every time (cheap 304 via ETag) so an upgrade never runs a stale script.
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app


def _ts(value: float) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value else None
