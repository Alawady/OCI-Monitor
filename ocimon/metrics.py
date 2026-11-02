"""Health signals from OCI Monitoring metrics that need no setup.

The Oracle Cloud Agent (CPU, memory), compute instance health (filesystem
status) and managed databases (storage, CPU, memory) publish these on their
own. One read-only SummarizeMetricsData query per metric and region covers
every resource, and the latest value becomes a health signal on the row.

Disk *free space* inside an instance is not here: OCI only sees volumes from
outside (I/O), so that needs an in-guest reporter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import oci

from .models import Resource


@dataclass(frozen=True)
class MetricSpec:
    key: str  # name in Resource.metrics
    namespace: str
    query: str  # MQL; the newest datapoint per resourceId is used
    types: frozenset[str]  # resource types it applies to
    states: frozenset[str]  # only resources in these states report data


@dataclass(frozen=True)
class Threshold:
    label: str
    warn: float | None
    crit: float | None = None
    unit: str = "%"


RUNNING = frozenset({"RUNNING"})
DB_UP = frozenset({"ACTIVE", "AVAILABLE", "AVAILABLE_NEEDS_ATTENTION"})

SPECS: list[MetricSpec] = [
    MetricSpec("cpu_pct", "oci_computeagent", "CpuUtilization[5m].mean()", frozenset({"instance"}), RUNNING),
    MetricSpec("memory_pct", "oci_computeagent", "MemoryUtilization[5m].mean()", frozenset({"instance"}), RUNNING),
    # Non-zero when the instance reports a filesystem problem (e.g. read-only or corrupt).
    MetricSpec("fs_status", "oci_compute_instance_health", "InstanceFileSystemStatus[5m].max()",
               frozenset({"instance"}), RUNNING),
    MetricSpec("storage_pct", "oci_mysql_database", "DbVolumeUtilization[5m].max()",
               frozenset({"mysql_db_system"}), DB_UP),
    MetricSpec("cpu_pct", "oci_mysql_database", "CPUUtilization[5m].mean()", frozenset({"mysql_db_system"}), DB_UP),
    MetricSpec("memory_pct", "oci_mysql_database", "MemoryUtilization[5m].mean()",
               frozenset({"mysql_db_system"}), DB_UP),
    MetricSpec("storage_pct", "oci_autonomous_database", "StorageUtilization[5m].max()",
               frozenset({"autonomous_database"}), DB_UP),
    MetricSpec("cpu_pct", "oci_autonomous_database", "CpuUtilization[5m].mean()",
               frozenset({"autonomous_database"}), DB_UP),
]

THRESHOLDS: dict[str, Threshold] = {
    "cpu_pct": Threshold("CPU", warn=90),
    "memory_pct": Threshold("Memory", warn=90, crit=97),
    "storage_pct": Threshold("Storage", warn=80, crit=90),
}

WINDOW = timedelta(minutes=30)
_RANK = {"OK": 0, "WARNING": 1, "CRITICAL": 2}


def evaluate(metrics: dict[str, float]) -> tuple[str | None, list[str]]:
    """Health ("OK"/"WARNING"/"CRITICAL", or None without data) and the reasons."""
    if not metrics:
        return None, []
    level, reasons = "OK", []
    for key, value in metrics.items():
        if key == "fs_status":
            if value > 0:
                level = "CRITICAL"
                reasons.append("Filesystem problem reported by the instance")
            continue
        t = THRESHOLDS.get(key)
        if t is None:
            continue
        hit = "CRITICAL" if t.crit is not None and value >= t.crit else "WARNING" if t.warn is not None and value >= t.warn else None
        if hit:
            limit = t.crit if hit == "CRITICAL" else t.warn
            reasons.append(f"{t.label} {value:.1f}{t.unit} (≥ {limit:g}{t.unit})")
            if _RANK[hit] > _RANK[level]:
                level = hit
    return level, reasons


def apply(resource: Resource, metrics: dict[str, float]) -> None:
    """Attach metric values and fold their health into the resource's own."""
    resource.metrics.update(metrics)
    level, reasons = evaluate(resource.metrics)
    resource.health_reasons = reasons
    if level is None:
        return
    if resource.health is None or _RANK.get(level, 0) > _RANK.get(resource.health, 0):
        resource.health = level


def latest_by_resource(series: list[Any]) -> dict[str, float]:
    """Newest datapoint per resourceId; several series for one resource keep the max."""
    out: dict[str, float] = {}
    for s in series:
        rid = (s.dimensions or {}).get("resourceId")
        if not rid or not s.aggregated_datapoints:
            continue
        value = max(s.aggregated_datapoints, key=lambda p: p.timestamp).value
        rid = rid.lower()
        out[rid] = max(out.get(rid, value), value)
    return out


def query_tasks(resources: list[Resource], root_id: str, tenancy_id: str, client: Callable[[], Any],
                specs: list[MetricSpec] = SPECS) -> list[tuple[MetricSpec, str, Callable[[], dict[str, float]]]]:
    """(spec, compartment, fn) for each metric that has matching resources in a region's resources.

    From the tenancy one subtree query covers everything; subtree queries aren't allowed
    elsewhere, so a sub-compartment root gets one query per compartment that has matches.
    """
    tasks = []
    for spec in specs:
        comps = sorted({r.compartment_id for r in resources if r.type in spec.types and r.state in spec.states})
        if not comps:
            continue
        subtree = root_id == tenancy_id
        for comp in [root_id] if subtree else comps:

            def run(spec: MetricSpec = spec, comp: str = comp) -> dict[str, float]:
                end = datetime.now(timezone.utc)
                details = oci.monitoring.models.SummarizeMetricsDataDetails(
                    namespace=spec.namespace, query=spec.query, start_time=end - WINDOW, end_time=end)
                data = client().summarize_metrics_data(comp, details, compartment_id_in_subtree=subtree).data
                return latest_by_resource(data)

            tasks.append((spec, comp, run))
    return tasks
