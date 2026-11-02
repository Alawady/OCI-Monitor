"""Registered collectors. Import a new collector module here to enable it."""

from __future__ import annotations

from ..models import ResourceType
from . import (  # noqa: F401  (imported for @register side effects)
    alarms,
    api_gateway,
    block_storage,
    compute,
    database,
    functions,
    load_balancer,
    network,
    object_storage,
    oke,
)
from .base import REGISTRY, Collector


def all_collectors() -> list[Collector]:
    return list(REGISTRY)


def resource_types() -> dict[str, ResourceType]:
    return {t.key: t for c in REGISTRY for t in c.types}
