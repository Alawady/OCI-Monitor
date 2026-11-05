"""One refresh of live OCI data: compartments -> Resource Search -> collectors."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

import oci

from . import metrics as metric_signals
from .auth import Auth
from .collectors import all_collectors
from .collectors.base import Collector, RegionContext, error_from_exception, list_all
from .models import Compartment, ErrorRecord, Resource
from .snapshot import Snapshot

log = logging.getLogger("ocimon.engine")

# Exponential backoff with jitter on throttling (429), 5xx, timeouts and connection errors.
RETRY_STRATEGY = oci.retry.RetryStrategyBuilder(
    max_attempts_check=True,
    max_attempts=6,
    total_elapsed_time_check=True,
    total_elapsed_time_seconds=120,
    retry_max_wait_between_calls_seconds=30,
    retry_base_sleep_time_seconds=1,
    service_error_check=True,
    service_error_retry_on_any_5xx=True,
    service_error_retry_config={429: []},
    backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE,
).get_retry_strategy()

CLIENT_TIMEOUT = (10, 60)  # connect, read seconds

ClientFactory = Callable[[type, str], Any]


class RefreshCancelled(Exception):
    """The app is shutting down; the partial result is discarded."""


class Collection:
    """State for a single refresh. Create a new one per run."""

    def __init__(self, auth: Auth, regions: list[str], root_id: str | None = None, *,
                 workers: int = 16, collectors: list[Collector] | None = None,
                 client_factory: ClientFactory | None = None,
                 shutdown_event: threading.Event | None = None,
                 metric_specs: list[metric_signals.MetricSpec] | None = None) -> None:
        self.auth = auth
        self._shutdown_event = shutdown_event
        self.tenancy_id = auth.tenancy_id
        self.root_id = root_id or auth.tenancy_id
        self.regions = regions
        self.workers = max(2, workers)
        self.collectors = collectors if collectors is not None else all_collectors()
        self.metric_specs = metric_signals.SPECS if metric_specs is None else metric_specs
        self._client_factory = client_factory or (
            lambda cls, region: auth.make_client(cls, region, retry_strategy=RETRY_STRATEGY,
                                                 timeout=CLIENT_TIMEOUT))
        self._local = threading.local()
        self._lock = threading.Lock()
        self._errors: dict[tuple, ErrorRecord] = {}
        self._search: dict[str, dict[str, list[Any]] | None] = {}
        self._ads: dict[str, list[str]] = {}
        self._namespace: str | None = None
        self.compartments: dict[str, Compartment] = {}
        self.tenancy_name: str | None = None
        self.io_pool: ThreadPoolExecutor | None = None

    # -- shared services used by RegionContext ------------------------------
    def stopping(self) -> bool:
        return self._shutdown_event is not None and self._shutdown_event.is_set()

    def client(self, client_cls: type, region: str) -> Any:
        # SDK clients hold a requests.Session; keep one per thread to be safe.
        cache = getattr(self._local, "clients", None)
        if cache is None:
            cache = self._local.clients = {}
        key = (client_cls, region)
        if key not in cache:
            cache[key] = self._client_factory(client_cls, region)
        return cache[key]

    def record_error(self, err: ErrorRecord) -> None:
        with self._lock:
            existing = self._errors.get(err.key())
            if existing:
                existing.count += 1
            else:
                self._errors[err.key()] = err

    def _record(self, exc: BaseException, region: str | None, service: str, operation: str,
                compartment_id: str | None = None) -> None:
        self.record_error(error_from_exception(exc, region=region, service=service,
                                               operation=operation, compartment_id=compartment_id))

    def search_ok(self, region: str) -> bool:
        return self._search.get(region) is not None

    def search_items(self, region: str, search_type: str) -> list[Any]:
        return (self._search.get(region) or {}).get(search_type, [])

    def availability_domains(self, region: str) -> list[str]:
        with self._lock:
            if region in self._ads:
                return self._ads[region]
        identity = self.client(oci.identity.IdentityClient, region)
        try:
            ads = [ad.name for ad in identity.list_availability_domains(self.tenancy_id).data]
        except Exception as exc:
            self._record(exc, region, "Identity", "ListAvailabilityDomains")
            ads = []
        with self._lock:
            self._ads[region] = ads
        return ads

    def namespace(self, region: str) -> str:
        with self._lock:
            if self._namespace:
                return self._namespace
        ns = self.client(oci.object_storage.ObjectStorageClient, region).get_namespace().data
        with self._lock:
            self._namespace = ns
        return ns

    # -- phases -------------------------------------------------------------
    def load_compartments(self) -> None:
        identity = self.client(oci.identity.IdentityClient, self.auth.region)
        root_name = "root"
        try:
            if self.root_id == self.tenancy_id:
                root_name = identity.get_tenancy(self.tenancy_id).data.name
                self.tenancy_name = root_name
            else:
                root_name = identity.get_compartment(self.root_id).data.name
        except Exception as exc:
            self._record(exc, None, "Identity", "GetCompartment", self.root_id)
        self.compartments = {self.root_id: Compartment(self.root_id, root_name, None)}

        try:
            if self.root_id == self.tenancy_id:
                found = list_all(identity.list_compartments, self.tenancy_id,
                                 compartment_id_in_subtree=True, access_level="ACCESSIBLE",
                                 lifecycle_state="ACTIVE")
            else:
                # compartment_id_in_subtree is only allowed on the tenancy; walk instead.
                found, frontier = [], [self.root_id]
                while frontier:
                    level = []
                    for parent in frontier:
                        level.extend(list_all(identity.list_compartments, parent,
                                              access_level="ACCESSIBLE", lifecycle_state="ACTIVE"))
                    found.extend(level)
                    frontier = [c.id for c in level]
        except Exception as exc:
            self._record(exc, None, "Identity", "ListCompartments", self.root_id)
            return
        for c in found:
            self.compartments[c.id] = Compartment(c.id, c.name, c.compartment_id, c.lifecycle_state,
                                                  c.description or "")

    def search_region(self, region: str) -> None:
        types = sorted({t for c in self.collectors for t in c.search_types})
        if not types:
            self._search[region] = {}
            return
        client = self.client(oci.resource_search.ResourceSearchClient, region)

        def query(type_names: list[str]) -> list[Any]:
            details = oci.resource_search.models.StructuredSearchDetails(
                query=f"query {', '.join(type_names)} resources", type="Structured")
            return list_all(client.search_resources, details, limit=1000)

        try:
            items = query(types)
        except oci.exceptions.ServiceError as exc:
            if exc.status != 400:
                self._record(exc, region, "Resource Search", "SearchResources")
                self._search[region] = None
                return
            # One unsupported type fails the whole query; retry type by type.
            items = []
            for t in types:
                try:
                    items.extend(query([t]))
                except Exception as inner:
                    self._record(inner, region, "Resource Search", f"SearchResources:{t}")
        except Exception as exc:
            self._record(exc, region, "Resource Search", "SearchResources")
            self._search[region] = None
            return

        by_type: dict[str, list[Any]] = {}
        for item in items:
            if item.compartment_id in self.compartments:
                by_type.setdefault(item.resource_type, []).append(item)
        self._search[region] = by_type

    def _run_collector(self, collector: Collector, region: str) -> tuple[list[Resource], float]:
        start = time.monotonic()
        try:
            resources = collector.collect(RegionContext(self, region, collector))
        except Exception as exc:
            resources = []
            if not self.stopping():  # pools refuse work during shutdown; that's not a collector bug
                log.exception("collector %s failed in %s", collector.key, region)
                self._record(exc, region, collector.label, "collect")
        return resources, time.monotonic() - start

    def _enrich_metrics(self, resources: list[Resource]) -> None:
        """Attach the latest Monitoring values (CPU, memory, storage...) and their health."""
        by_region: dict[str, list[Resource]] = defaultdict(list)
        for r in resources:
            by_region[r.region].append(r)
        jobs = []
        for region, items in by_region.items():
            client = lambda region=region: self.client(oci.monitoring.MonitoringClient, region)  # noqa: E731
            for spec, comp, fn in metric_signals.query_tasks(items, self.root_id, self.tenancy_id, client,
                                                             self.metric_specs):
                jobs.append((region, spec, comp, self.io_pool.submit(fn)))
        values: dict[str, dict[str, float]] = defaultdict(dict)
        for region, spec, comp, future in jobs:
            try:
                found = future.result()
            except Exception as exc:
                if not self.stopping():
                    self._record(exc, region, "Monitoring", f"SummarizeMetricsData:{spec.namespace}",
                                 None if comp == self.root_id else comp)
                continue
            for rid, value in found.items():
                values[rid][spec.key] = value if spec.key.startswith("_") else round(value, 1)
        for r in resources:
            found = values.get(r.id.lower())
            if found:
                metric_signals.apply(r, found)

    def _search_fallbacks(self, seen: set[str]) -> list[Resource]:
        """Rows for resources Search found but enrichment couldn't read."""
        by_search_type = {t.search_type: t for c in self.collectors for t in c.types if t.search_type}
        out = []
        for region, by_type in self._search.items():
            for search_type, items in (by_type or {}).items():
                rtype = by_search_type.get(search_type)
                if not rtype:
                    continue
                for item in items:
                    if item.identifier in seen:
                        continue
                    seen.add(item.identifier)
                    out.append(Resource(
                        id=item.identifier, name=item.display_name, type=rtype.key, region=region,
                        compartment_id=item.compartment_id, state=item.lifecycle_state,
                        time_created=item.time_created, source="search",
                        details={"note": "Details unavailable; listed from Resource Search"},
                    ))
        return out

    def run(self) -> Snapshot:
        started = time.time()
        t0 = time.monotonic()
        self.auth.reload()
        if self.auth.token_expired():
            expired = datetime.fromtimestamp(self.auth.token_expires_at, timezone.utc).isoformat()
            self.record_error(ErrorRecord(
                None, "Authentication", "SessionToken",
                f"Session token expired at {expired}. Run `oci session refresh --profile {self.auth.profile}`.",
                kind="permission"))

        with ThreadPoolExecutor(self.workers, thread_name_prefix="oci-io") as io, \
                ThreadPoolExecutor(min(8, self.workers), thread_name_prefix="oci-collect") as orch:
            self.io_pool = io
            self.load_compartments()
            list(io.map(self.search_region, self.regions))
            jobs = [(c, r, orch.submit(self._run_collector, c, r))
                    for r in self.regions for c in self.collectors]
            resources: dict[str, Resource] = {}
            timings: dict[str, float] = {}
            for collector, region, future in jobs:
                found, seconds = future.result()
                timings[collector.key] = round(timings.get(collector.key, 0.0) + seconds, 2)
                for r in found:
                    resources.setdefault(r.id, r)
            if not self.stopping():
                self._enrich_metrics(list(resources.values()))

        if self.stopping():
            raise RefreshCancelled()
        fallbacks = self._search_fallbacks(set(resources))
        all_resources = list(resources.values()) + fallbacks
        stats = {
            "search_items": sum(len(v) for by in self._search.values() for v in (by or {}).values()),
            "search_fallbacks": len(fallbacks),
            "collector_seconds": timings,
        }
        return Snapshot(
            resources=all_resources,
            compartments=self.compartments,
            root_id=self.root_id,
            regions=self.regions,
            errors=sorted(self._errors.values(), key=lambda e: (e.region or "", e.service, e.operation)),
            started_at=started,
            finished_at=time.time(),
            tenancy_name=self.tenancy_name,
            mode="live",
            stats=stats,
            elapsed=time.monotonic() - t0,
        )


class LiveSource:
    """Long-lived wrapper that resolves regions once and builds a Collection per refresh."""

    def __init__(self, auth: Auth, regions_spec: str | None = None, root_id: str | None = None,
                 workers: int = 16, client_factory: ClientFactory | None = None) -> None:
        self.auth = auth
        self.regions_spec = (regions_spec or "").strip()
        self.root_id = root_id
        self.workers = workers
        self.client_factory = client_factory
        self.shutdown_event: threading.Event | None = None  # set by the Refresher so shutdown can cancel a run
        self._regions: list[str] | None = None

    def regions(self) -> list[str]:
        if self._regions is not None:
            return self._regions
        spec = self.regions_spec
        if not spec:
            return [self.auth.region]
        if spec.lower() != "all":
            return [r.strip() for r in spec.split(",") if r.strip()]
        factory = self.client_factory or (lambda cls, region: self.auth.make_client(
            cls, region, retry_strategy=RETRY_STRATEGY, timeout=CLIENT_TIMEOUT))
        identity = factory(oci.identity.IdentityClient, self.auth.region)
        subs = identity.list_region_subscriptions(self.auth.tenancy_id).data
        ready = [s for s in subs if s.status == "READY"]
        ready.sort(key=lambda s: (not s.is_home_region, s.region_name))
        self._regions = [s.region_name for s in ready]
        return self._regions

    def __call__(self) -> Snapshot:
        err = None
        try:
            regions = self.regions()
        except Exception as exc:
            # Couldn't list subscriptions: fall back to the profile region and say so.
            regions = [self.auth.region]
            err = error_from_exception(exc, region=None, service="Identity",
                                       operation="ListRegionSubscriptions", compartment_id=None)
        collection = Collection(self.auth, regions, self.root_id, workers=self.workers,
                                client_factory=self.client_factory, shutdown_event=self.shutdown_event)
        if err:
            collection.record_error(err)
        return collection.run()
