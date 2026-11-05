"""Cost data from the OCI Usage API.

Fetched by its own background Refresher (hourly by default) because cost data
only changes a few times a day. Two queries per refresh, both from the home
region:

- 60 days by service / compartment / region: charts, totals, comparisons.
- 31 days by resource: the per-resource cost column and the top-resources list.

Everything the API needs is pre-aggregated here, so serving the Cost tab or
adding cost to resource rows is in-memory work only.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import cached_property
from typing import Any, Callable

import oci

from .auth import Auth
from .collectors.base import list_all
from .engine import CLIENT_TIMEOUT, RETRY_STRATEGY
from .models import Resource
from .snapshot import Snapshot

log = logging.getLogger("ocimon.cost")

SERVICE_DAYS = 60  # enough to compare the last 30 days with the 30 before
RESOURCE_DAYS = 31  # 30 full days + today
RANGES = (7, 14, 30)
SERIES_SLOTS = 7  # top services get a fixed color slot; the rest fold into "Other"
OTHER = "Other"
PERMISSION_HINT = "Allow group <your-group> to read usage-reports in tenancy"


@dataclass(frozen=True)
class CostRow:
    day: date
    service: str
    compartment_id: str
    region: str
    amount: float


@dataclass(frozen=True)
class ResourceCostRow:
    day: date
    resource_id: str  # lower-cased: the Usage API sometimes upper-cases OCIDs
    service: str
    compartment_id: str
    region: str
    amount: float


@dataclass
class CostReport:
    rows: list[CostRow]
    resource_rows: list[ResourceCostRow]
    currency: str
    today: date  # UTC; the Usage API works in UTC days and today's data is partial
    started_at: float = 0.0
    finished_at: float = 0.0
    elapsed: float = 0.0
    error: str | None = None
    error_kind: str | None = None  # "permission" or "error"
    mode: str = "live"

    def log_summary(self) -> str:
        if self.error:
            return f"cost unavailable ({self.error_kind}): {self.error}"
        return (f"{len(self.rows)} service rows, {len(self.resource_rows)} resource rows "
                f"in {self.elapsed:.1f}s")

    @cached_property
    def service_slots(self) -> dict[str, int]:
        """Stable color slot per service: rank over the whole tenancy and window.

        Computed once per report and independent of filters, so a service keeps
        its color when the compartment or region selection changes.
        """
        totals: dict[str, float] = defaultdict(float)
        for r in self.rows:
            totals[r.service] += r.amount
        ranked = sorted((s for s in totals if totals[s] > 0), key=lambda s: -totals[s])
        return {s: i + 1 for i, s in enumerate(ranked[:SERIES_SLOTS])}

    @cached_property
    def resource_week(self) -> dict[str, list[float]]:
        """Daily cost over the last 7 full days per billing key (see billing_key)."""
        days = [self.today - timedelta(days=i) for i in range(7, 0, -1)]
        index = {d: i for i, d in enumerate(days)}
        daily: dict[str, list[float]] = {}
        for r in self.resource_rows:
            i = index.get(r.day)
            if i is not None:
                daily.setdefault(billing_key(r.resource_id, r.region), [0.0] * 7)[i] += r.amount
        return daily

    def for_resource(self, resource: Resource) -> dict[str, Any] | None:
        """Last-7-day cost for a dashboard resource, summed over all its billing keys."""
        found = [self.resource_week[k] for k in billing_keys(resource) if k in self.resource_week]
        if not found:
            return None
        vals = [sum(day) for day in zip(*found)]
        if sum(vals) <= 0:
            return None
        return {"daily": [round(v, 2) for v in vals], "total_7d": round(sum(vals), 2),
                "avg_day": round(sum(vals) / 7, 2)}


def billing_key(resource_id: str, region: str) -> str:
    """How a Usage API row identifies what it bills. OCIDs are global; anything else (bucket
    names) is only unique within a region, so it's scoped by region."""
    rid = (resource_id or "").strip().lower()
    return rid if rid.startswith("ocid1.") else f"{region}|{rid}"


def billing_keys(resource: Resource) -> list[str]:
    """Every key the Usage API may bill a dashboard resource under."""
    keys = [resource.id.lower()]
    if resource.type == "bucket":
        # Object Storage bills buckets as "namespace/bucket", and sometimes as just "bucket".
        name = (resource.refs.get("bucket") or resource.name).lower()
        ns = (resource.refs.get("namespace") or "").lower()
        keys += [f"{resource.region}|{ns}/{name}", f"{resource.region}|{name}"] if ns else [f"{resource.region}|{name}"]
    return keys


# --- Live source ---------------------------------------------------------------

class LiveCostSource:
    def __init__(self, auth: Auth, client_factory: Callable[[type, str], Any] | None = None) -> None:
        self.auth = auth
        self.client_factory = client_factory or (lambda cls, region: auth.make_client(
            cls, region, retry_strategy=RETRY_STRATEGY, timeout=CLIENT_TIMEOUT))
        self.shutdown_event: threading.Event | None = None  # set by the Refresher
        self._home_region: str | None = None

    def home_region(self) -> str:
        """The Usage API is served from the tenancy's home region."""
        if self._home_region is None:
            try:
                identity = self.client_factory(oci.identity.IdentityClient, self.auth.region)
                subs = identity.list_region_subscriptions(self.auth.tenancy_id).data
                self._home_region = next(s.region_name for s in subs if s.is_home_region)
            except Exception:
                return self.auth.region  # try the profile region; retry the lookup next time
        return self._home_region

    def _query(self, client: Any, start: datetime, end: datetime, group_by: list[str]) -> list[Any]:
        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=self.auth.tenancy_id, time_usage_started=start, time_usage_ended=end,
            granularity="DAILY", query_type="COST", group_by=group_by, compartment_depth=7)
        return list_all(client.request_summarized_usages, details, limit=1000)

    def __call__(self) -> CostReport:
        started, t0 = time.time(), time.monotonic()
        self.auth.reload()
        now = datetime.now(timezone.utc)
        today = now.date()
        end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) + timedelta(days=1)
        client = self.client_factory(oci.usage_api.UsageapiClient, self.home_region())
        try:
            service_items = self._query(client, end - timedelta(days=SERVICE_DAYS), end,
                                        ["service", "compartmentId", "region"])
            resource_items = self._query(client, end - timedelta(days=RESOURCE_DAYS), end,
                                         ["resourceId", "service", "compartmentId", "region"])
        except oci.exceptions.ServiceError as exc:
            if exc.status not in (401, 403, 404):
                raise
            return CostReport([], [], "", today, started, time.time(), time.monotonic() - t0,
                              error=f"No permission to read cost data ({exc.status} {exc.code}). "
                                    f"Policy needed: {PERMISSION_HINT}",
                              error_kind="permission")

        currencies: dict[str, int] = defaultdict(int)
        rows = []
        for i in service_items:
            amount = i.computed_amount or 0.0
            if (i.currency or "").strip():
                currencies[i.currency.strip()] += 1
            if amount:
                rows.append(CostRow(i.time_usage_started.date(), i.service or "Unknown",
                                    i.compartment_id or "", i.region or "", amount))
        resource_rows = [
            ResourceCostRow(i.time_usage_started.date(), (i.resource_id or "").strip().lower(),
                            i.service or "Unknown", i.compartment_id or "", i.region or "",
                            i.computed_amount)
            for i in resource_items if i.computed_amount
        ]
        currency = max(currencies, key=currencies.get) if currencies else ""
        return CostReport(rows, resource_rows, currency, today, started, time.time(), time.monotonic() - t0)


# --- Demo source ---------------------------------------------------------------

# (daily cost range, service) per demo resource type
_DEMO_RATES = {
    "instance": ((4, 38), "Compute"),
    "boot_volume": ((0.4, 1.6), "Block Storage"),
    "volume": ((1, 9), "Block Storage"),
    "load_balancer": ((3, 6), "Load Balancer"),
    "network_load_balancer": ((0.5, 1), "Network Load Balancer"),
    "autonomous_database": ((12, 60), "Database"),
    "db_system": ((40, 55), "Database"),
    "mysql_db_system": ((8, 45), "MySQL"),
    "postgresql_db_system": ((9, 12), "OCI Database Service with PostgreSQL"),
    "cache_cluster": ((18, 20), "OCI Cache Service"),
    "oke_cluster": ((2.4, 2.4), "Container Engine for Kubernetes"),
    "bucket": ((0.1, 6), "Object Storage"),
    "functions_application": ((0.05, 0.6), "Functions"),
    "api_gateway": ((0.5, 2), "API Gateway"),
    "nat_gateway": ((1, 1.5), "Virtual Cloud Network"),
}


class DemoCostSource:
    """Deterministic fake costs derived from the demo inventory."""

    def __init__(self, snapshot_fn: Callable[[], Snapshot | None], delay: float = 0.4) -> None:
        self.snapshot_fn = snapshot_fn
        self.delay = delay

    def __call__(self) -> CostReport:
        started, t0 = time.time(), time.monotonic()
        time.sleep(self.delay)
        from .demo import build_demo
        snap = self.snapshot_fn() or build_demo(0)
        today = datetime.now(timezone.utc).date()
        rng = random.Random(7)
        rows, resource_rows = [], []
        for r in snap.resources:
            if r.type not in _DEMO_RATES or r.terminated:
                continue
            (lo, hi), service = _DEMO_RATES[r.type]
            base = rng.uniform(lo, hi)
            if r.type == "instance":
                base = (r.details.get("ocpus") or 1) * 1.9 + (r.details.get("memory_gb") or 8) * 0.06
            base *= 0.15 if r.state in ("STOPPED", "INACTIVE") else 1
            for back in range(SERVICE_DAYS):
                day = today - timedelta(days=back)
                amount = base * (1 + 0.08 * math.sin(back / 2.3)) * rng.uniform(0.95, 1.05)
                if r.type == "oke_cluster" and back < 12:
                    amount *= 6  # cluster scaled up 12 days ago: a visible step in the chart
                if back == 0:
                    amount *= 0.45  # today is partial
                rows.append(CostRow(day, service, r.compartment_id, r.region, round(amount, 4)))
                if back < RESOURCE_DAYS:
                    # Like OCI: buckets are billed by "namespace/name", everything else by OCID.
                    rid = f"{r.refs.get('namespace')}/{r.name}" if r.type == "bucket" else r.id.lower()
                    resource_rows.append(ResourceCostRow(day, rid, service, r.compartment_id,
                                                         r.region, round(amount, 4)))
        # Spend not tied to anything on the dashboard: backups, egress, logging.
        shared = [("Block Storage", "ocid1.volumebackup.oc1.me-jeddah-1.demo", 14.0),
                  ("Virtual Cloud Network", "ocid1.vnic.oc1.me-jeddah-1.demo", 3.5),
                  ("Logging", "ocid1.log.oc1.me-jeddah-1.demo", 0.8)]
        for service, rid, base in shared:
            for back in range(SERVICE_DAYS):
                day = today - timedelta(days=back)
                amount = base * rng.uniform(0.9, 1.1) * (0.45 if back == 0 else 1)
                rows.append(CostRow(day, service, snap.root_id, "me-jeddah-1", round(amount, 4)))
                if back < RESOURCE_DAYS:
                    resource_rows.append(ResourceCostRow(day, rid, service, snap.root_id, "me-jeddah-1",
                                                         round(amount, 4)))
        return CostReport(rows, resource_rows, "USD", today, started, time.time(), time.monotonic() - t0,
                          mode="demo")


# --- Views ---------------------------------------------------------------------

_OCID_KINDS = {
    "volumebackup": "Volume backup", "bootvolumebackup": "Boot volume backup", "vnic": "VNIC",
    "log": "Log", "alarm": "Alarm", "image": "Custom image", "mysqlinstance": "MySQL instance",
    "bootvolumereplica": "Boot volume replica", "webappfirewall": "Web application firewall",
    "emailsender": "Email sender", "bucket": "Bucket", "rediscluster": "OCI Cache cluster",
    "postgresqldbsystem": "PostgreSQL DB system", "drprotectiongroup": "DR protection group",
}


def _ocid_label(resource_id: str) -> str:
    resource_id = resource_id.split("|", 1)[-1]  # drop the region scope of non-OCID keys
    parts = resource_id.split(".")
    if len(parts) > 2 and parts[0] == "ocid1":
        # The OCID tail tells apart several items of the same kind.
        return f"{_OCID_KINDS.get(parts[1], parts[1].replace('_', ' ').capitalize())} …{resource_id[-6:]}"
    return resource_id or "Unattributed"


def _pct(now: float, before: float) -> float | None:
    return round((now - before) / before * 100, 1) if before > 0 else None


@dataclass
class _Window:
    days: int
    today: date
    full_days: list[date] = field(init=False)
    previous: tuple[date, date] = field(init=False)

    def __post_init__(self) -> None:
        self.full_days = [self.today - timedelta(days=i) for i in range(self.days, 0, -1)]
        self.previous = (self.today - timedelta(days=2 * self.days), self.today - timedelta(days=self.days + 1))


def cost_view(report: CostReport, snapshot: Snapshot | None, days: int = 7,
              compartment: str | None = None, region: str | None = None) -> dict[str, Any]:
    days = days if days in RANGES else 7
    w = _Window(days, report.today)
    regions = {r for r in (region or "").split(",") if r}

    allowed = None
    if compartment and snapshot is not None:
        allowed = snapshot.descendants(compartment)

    def keep(compartment_id: str, row_region: str | None = None) -> bool:
        if allowed is not None and compartment_id not in allowed:
            return False
        return not (regions and row_region is not None and row_region not in regions)

    slots = report.service_slots
    series_of = lambda service: service if service in slots else OTHER  # noqa: E731
    chart_days = w.full_days + [report.today]
    chart_index = {d: i for i, d in enumerate(chart_days)}
    chart = [defaultdict(float) for _ in chart_days]
    period_by_service: dict[str, float] = defaultdict(float)
    previous_by_service: dict[str, float] = defaultdict(float)
    by_compartment: dict[str, float] = defaultdict(float)
    prev_start, prev_end = w.previous
    first_full = w.full_days[0]

    for r in report.rows:
        if not keep(r.compartment_id, r.region):
            continue
        i = chart_index.get(r.day)
        if i is not None:
            chart[i][series_of(r.service)] += r.amount
        if first_full <= r.day < report.today:
            period_by_service[r.service] += r.amount
            by_compartment[r.compartment_id] += r.amount
        elif prev_start <= r.day <= prev_end:
            previous_by_service[r.service] += r.amount

    total = sum(period_by_service.values())
    previous = sum(previous_by_service.values())
    daily_totals = [sum(d.values()) for d in chart]

    series = [s for s, _ in sorted(slots.items(), key=lambda kv: kv[1])]
    if any(OTHER in d for d in chart):
        series.append(OTHER)

    services = sorted(period_by_service, key=lambda s: -period_by_service[s])
    by_service = [{
        "service": s,
        "slot": slots.get(s),
        "amount": round(period_by_service[s], 2),
        "share": round(period_by_service[s] / total * 100, 1) if total else 0,
        "previous": round(previous_by_service.get(s, 0.0), 2),
        "delta_pct": _pct(period_by_service[s], previous_by_service.get(s, 0.0)),
    } for s in services if period_by_service[s] >= 0.005]

    # Compartments: the selected one's own spend plus one rolled-up row per child.
    compartments = []
    if snapshot is not None and snapshot.compartments:
        root = compartment if compartment in snapshot.compartments else snapshot.root_id
        groups: dict[str, float] = defaultdict(float)
        owner = {}
        for child in snapshot.children.get(root, []):
            for cid in snapshot.descendants(child):
                owner[cid] = child
        for cid, amount in by_compartment.items():
            if cid == root:
                groups[root] += amount
            elif cid in owner:
                groups[owner[cid]] += amount
            elif allowed is None:
                groups[""] += amount  # deleted or inaccessible compartments
        for cid, amount in sorted(groups.items(), key=lambda kv: -kv[1]):
            if amount < 0.005:
                continue
            node = snapshot.compartments.get(cid)
            compartments.append({
                "id": cid or None,
                "name": (f"{node.name} (directly)" if cid == root else node.name) if node
                else "Other compartments (deleted or not accessible)",
                "path": snapshot.paths.get(cid) if node else None,
                "amount": round(amount, 2),
                "share": round(amount / total * 100, 1) if total else 0,
                "drilldown": bool(node) and cid != root,
            })

    top_resources, covered = _top_resources(report, snapshot, w, keep)

    return {
        "status": "error" if report.error else "ok",
        "currency": report.currency,
        "days": days,
        "ranges": list(RANGES),
        "today": report.today.isoformat(),
        "totals": {
            "period": round(total, 2),
            "previous": round(previous, 2),
            "delta_pct": _pct(total, previous),
            "daily_avg": round(total / days, 2),
            "yesterday": round(daily_totals[-2], 2) if len(daily_totals) > 1 else 0,
            "today_partial": round(daily_totals[-1], 2),
            "covered_pct": covered,
        },
        "series": [{"name": s, "slot": slots.get(s)} for s in series],
        "chart": [{
            "date": d.isoformat(),
            "partial": d == report.today,
            "total": round(daily_totals[i], 2),
            "values": {k: round(v, 2) for k, v in chart[i].items() if v >= 0.005},
        } for i, d in enumerate(chart_days)],
        "by_service": by_service,
        "by_compartment": compartments,
        "top_resources": top_resources,
    }


def _top_resources(report: CostReport, snapshot: Snapshot | None, w: _Window,
                   keep: Callable[..., bool]) -> tuple[list[dict[str, Any]], float | None]:
    if w.days > RESOURCE_DAYS - 1:
        return [], None
    index = {d: i for i, d in enumerate(w.full_days)}
    daily: dict[str, list[float]] = {}
    service: dict[str, str] = {}
    compartment: dict[str, str] = {}
    # Billing keys that belong to a dashboard resource are grouped under its id.
    owner = {key: res for res in (snapshot.resources if snapshot else []) for key in billing_keys(res)}
    known = {res.id.lower(): res for res in owner.values()}
    for r in report.resource_rows:
        i = index.get(r.day)
        if i is None or not keep(r.compartment_id, r.region):
            continue
        key = billing_key(r.resource_id, r.region)
        res = owner.get(key)
        rid = res.id.lower() if res else key
        daily.setdefault(rid, [0.0] * w.days)[i] += r.amount
        service.setdefault(rid, r.service)
        compartment.setdefault(rid, r.compartment_id)

    totals = {rid: sum(v) for rid, v in daily.items()}
    grand = sum(totals.values())
    covered = round(sum(t for rid, t in totals.items() if rid in known) / grand * 100, 1) if grand else None

    out = []
    for rid in sorted(totals, key=lambda k: -totals[k])[:10]:
        res = known.get(rid)
        cid = res.compartment_id if res else compartment.get(rid, "")
        out.append({
            "id": res.id if res else rid,
            "name": res.name if res else _ocid_label(rid),
            "type": res.type if res else None,
            "on_dashboard": res is not None,
            "service": service.get(rid),
            "compartment_path": snapshot.paths.get(cid) if snapshot else None,
            "amount": round(totals[rid], 2),
            "share": round(totals[rid] / grand * 100, 1) if grand else 0,
            "daily": [round(v, 2) for v in daily[rid]],
        })
    return out, covered
