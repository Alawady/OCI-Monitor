"""Collector plug-in interface.

A collector turns one OCI service's list/get responses into `Resource`s for a
single region. To add one: subclass `Collector`, declare its `types`, decorate
it with `@register`, and import the module in `collectors/__init__.py`.

Collectors must only call read operations (list_*, get_*). Every call goes
through `RegionContext`, which runs it on the shared thread pool and turns
failures into `ErrorRecord`s so one denied compartment never breaks a refresh.
"""

from __future__ import annotations

from concurrent.futures import Executor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Hashable, Iterable

import oci

from ..models import Compartment, ErrorRecord, Resource, ResourceType

if TYPE_CHECKING:
    from ..engine import Collection


def list_all(method: Callable[..., Any], *args: Any, **kwargs: Any) -> list[Any]:
    """Follow opc-next-page tokens. Works for list responses and *Collection.items."""
    items: list[Any] = []
    page = None
    while True:
        response = method(*args, page=page, **kwargs) if page else method(*args, **kwargs)
        data = response.data
        items.extend(data if isinstance(data, list) else (getattr(data, "items", None) or []))
        page = getattr(response, "next_page", None)
        if not page:
            return items


def short_ad(ad: str | None) -> str | None:
    """'Uocm:ME-JEDDAH-1-AD-1' -> 'AD-1'."""
    if not ad:
        return None
    return "AD-" + ad.rsplit("-", 1)[-1] if "-AD-" in ad else ad


def error_from_exception(exc: BaseException, *, region: str | None, service: str,
                         operation: str, compartment_id: str | None) -> ErrorRecord:
    if isinstance(exc, oci.exceptions.ServiceError):
        kind = "permission" if exc.status in (401, 403, 404) else "error"
        message = exc.message or exc.code or "Service error"
        if exc.status == 404 and kind == "permission":
            message = f"{message} (not found or not authorized)"
        return ErrorRecord(region, service, operation, str(message)[:500], compartment_id,
                           exc.status, exc.code, kind)
    return ErrorRecord(region, service, operation, f"{type(exc).__name__}: {str(exc)[:400]}",
                       compartment_id, None, None, "error")


@dataclass
class Task:
    key: Hashable
    operation: str  # e.g. "ListInstances"; shown in the errors panel
    fn: Callable[[], Any]
    compartment_id: str | None = None


class RegionContext:
    """Everything a collector may touch while collecting one region."""

    def __init__(self, run: "Collection", region: str, collector: "Collector") -> None:
        self._run = run
        self.region = region
        self.collector = collector

    # -- environment --------------------------------------------------------
    @property
    def tenancy_id(self) -> str:
        return self._run.tenancy_id

    @property
    def root_id(self) -> str:
        return self._run.root_id

    @property
    def compartments(self) -> dict[str, Compartment]:
        return self._run.compartments

    @property
    def search_ok(self) -> bool:
        return self._run.search_ok(self.region)

    def client(self, client_cls: type) -> Any:
        return self._run.client(client_cls, self.region)

    def search_items(self, search_type: str) -> list[Any]:
        return self._run.search_items(self.region, search_type)

    def compartments_with(self, *search_types: str) -> list[str]:
        """Compartments where Search saw any of these types in this region.

        If Search failed for the region we can't know, so return all of them.
        """
        if not self.search_ok:
            return list(self.compartments)
        found = {item.compartment_id for t in search_types for item in self.search_items(t)}
        return [c for c in self.compartments if c in found]

    def region_has(self, *search_types: str) -> bool:
        return not self.search_ok or any(self.search_items(t) for t in search_types)

    def availability_domains(self) -> list[str]:
        return self._run.availability_domains(self.region)

    def namespace(self) -> str:
        return self._run.namespace(self.region)

    # -- calls --------------------------------------------------------------
    def record(self, exc: BaseException, operation: str, compartment_id: str | None = None) -> None:
        self._run.record_error(error_from_exception(
            exc, region=self.region, service=self.collector.label,
            operation=operation, compartment_id=compartment_id))

    def run(self, tasks: Iterable[Task]) -> dict[Hashable, Any]:
        """Run tasks in parallel; return {key: result} for the ones that succeeded."""
        tasks = list(tasks)
        if self._run.stopping():
            return {}
        pool: Executor = self._run.io_pool
        futures = [(t, pool.submit(t.fn)) for t in tasks]
        results: dict[Hashable, Any] = {}
        for task, future in futures:
            try:
                results[task.key] = future.result()
            except Exception as exc:
                self.record(exc, task.operation, task.compartment_id)
        return results

    def per_compartment(self, operation: str, compartments: Iterable[str],
                        fn: Callable[[str], Any]) -> dict[str, Any]:
        return self.run(Task(c, operation, (lambda c=c: fn(c)), c) for c in compartments)

    def map(self, operation: str, items: Iterable[Any], fn: Callable[[Any], Any],
            key: Callable[[Any], Hashable] = lambda x: x,
            compartment: Callable[[Any], str | None] = lambda x: None) -> dict[Hashable, Any]:
        return self.run(Task(key(i), operation, (lambda i=i: fn(i)), compartment(i)) for i in items)


class Collector:
    key: str = ""
    label: str = ""
    types: tuple[ResourceType, ...] = ()

    @property
    def search_types(self) -> list[str]:
        return [t.search_type for t in self.types if t.search_type]

    def collect(self, ctx: RegionContext) -> list[Resource]:
        raise NotImplementedError


REGISTRY: list[Collector] = []


def register(cls: type[Collector]) -> type[Collector]:
    REGISTRY.append(cls())
    return cls
