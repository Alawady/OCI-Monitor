"""Normalized data model shared by collectors, the cache, the API and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# --- Categories -------------------------------------------------------------

@dataclass(frozen=True)
class Category:
    key: str
    label: str  # section heading in the main area
    card: str  # short label for the summary card


CATEGORIES: list[Category] = [
    Category("compute", "Compute", "Instances"),
    Category("storage", "Block Storage", "Volumes"),
    Category("load_balancing", "Load Balancing", "Load Balancers"),
    Category("database", "Databases", "Databases"),
    Category("containers", "Kubernetes (OKE)", "OKE"),
    Category("serverless", "Functions & API Gateway", "Serverless"),
    Category("object_storage", "Object Storage", "Buckets"),
    Category("networking", "Networking", "Networking"),
]


@dataclass(frozen=True)
class ResourceType:
    """A kind of resource a collector produces.

    `search_type` is the Resource Search type name used to locate it (None if
    Search doesn't index it). `console` is a path template under the OCI
    Console; it may reference {id}, {region} and any key of Resource.refs.
    """

    key: str
    label: str
    category: str
    search_type: str | None = None
    console: str | None = None


# --- State classification ---------------------------------------------------

OK_STATES = {
    "RUNNING", "AVAILABLE", "ACTIVE", "OK", "ATTACHED", "SUCCEEDED", "ENABLED",
}
STOPPED_STATES = {"STOPPED", "INACTIVE", "DISABLED", "DETACHED", "SUSPENDED", "SUPPRESSED"}
BAD_STATES = {"FAILED", "TERMINATED", "CRITICAL", "DELETED", "ERROR"}
WARN_STATES = {
    "PROVISIONING", "STARTING", "STOPPING", "UPDATING", "WARNING", "CREATING",
    "RESTORING", "SCALING", "TERMINATING", "DELETING", "MOVING", "UPGRADING",
    "MAINTENANCE_IN_PROGRESS", "BACKUP_IN_PROGRESS", "ROLE_CHANGE_IN_PROGRESS",
    "AVAILABLE_NEEDS_ATTENTION", "NEEDS_ATTENTION", "UNAVAILABLE", "INACCESSIBLE",
    "DEGRADED", "PENDING", "RESTARTING", "STANDBY", "FIRING",
}
# Amber states that should still count as a problem, not just "in progress".
ATTENTION_STATES = {
    "WARNING", "AVAILABLE_NEEDS_ATTENTION", "NEEDS_ATTENTION", "UNAVAILABLE",
    "INACCESSIBLE", "DEGRADED",
}
# Resources that are gone on purpose: shown red, but never counted as problems.
GONE_STATES = {"TERMINATED", "DELETED"}

_CLASS_RANK = {"ok": 0, "unknown": 1, "stopped": 2, "warn": 3, "bad": 4}


def normalize_state(value: Any) -> str:
    """Resource Search mixes 'ACTIVE', 'Active' and 'active'; SDK enums are upper."""
    if value is None or value == "":
        return "UNKNOWN"
    return str(value).strip().upper().replace(" ", "_").replace("-", "_")


def state_class(state: str | None) -> str:
    if not state:
        return "unknown"
    if state in OK_STATES:
        return "ok"
    if state in BAD_STATES:
        return "bad"
    if state in WARN_STATES or state.endswith("ING"):
        return "warn"
    if state in STOPPED_STATES:
        return "stopped"
    return "unknown"


def worst_class(*classes: str) -> str:
    return max(classes, key=lambda c: _CLASS_RANK.get(c, 1))


# --- Resources ---------------------------------------------------------------

@dataclass
class Resource:
    id: str
    name: str
    type: str
    region: str
    compartment_id: str
    state: str = "UNKNOWN"
    health: str | None = None  # OK / WARNING / CRITICAL / UNKNOWN where the service reports it
    time_created: datetime | str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    # OCIDs of related resources (vcn_id, instance_id, cluster_id...) plus other
    # values the console URL template needs. Resolved to names by the API.
    refs: dict[str, Any] = field(default_factory=dict)
    source: str = "api"  # "api" (enriched) or "search" (fallback from Resource Search)
    # Latest OCI Monitoring values, e.g. {"cpu_pct": 12.5, "memory_pct": 93.6} (see metrics.py)
    metrics: dict[str, float] = field(default_factory=dict)
    health_reasons: list[str] = field(default_factory=list)  # why health is WARNING/CRITICAL

    def __post_init__(self) -> None:
        self.state = normalize_state(self.state)
        if self.health is not None:
            self.health = normalize_state(self.health)
        self.name = self.name or self.id

    @property
    def state_class(self) -> str:
        return state_class(self.state)

    @property
    def health_class(self) -> str | None:
        return state_class(self.health) if self.health else None

    @property
    def status(self) -> str:
        """Overall color class: the worse of lifecycle state and health."""
        if self.health and self.state not in GONE_STATES:
            return worst_class(self.state_class, self.health_class or "unknown")
        return self.state_class

    @property
    def problem(self) -> str | None:
        """'critical', 'warning' or None. Terminated/deleted never count."""
        if self.state in GONE_STATES:
            return None
        if self.state_class == "bad" or self.health == "CRITICAL":
            return "critical"
        if self.health == "WARNING" or self.state in ATTENTION_STATES:
            return "warning"
        return None

    @property
    def terminated(self) -> bool:
        return self.state in GONE_STATES


@dataclass
class Compartment:
    id: str
    name: str
    parent_id: str | None
    lifecycle_state: str = "ACTIVE"
    description: str = ""


@dataclass
class ErrorRecord:
    """Something that could not be read. Never contains credentials."""

    region: str | None
    service: str
    operation: str
    message: str
    compartment_id: str | None = None
    status: int | None = None
    code: str | None = None
    kind: str = "error"  # "permission" (401/403/404) or "error"
    count: int = 1

    def key(self) -> tuple:
        return (self.region, self.service, self.operation, self.compartment_id, self.status, self.code)


def isoformat(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
