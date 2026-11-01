"""Fakes for the OCI SDK so collectors run without network access."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable

import oci
import pytest

from ocimon.collectors.base import RegionContext
from ocimon.engine import Collection
from ocimon.models import Compartment

TENANCY = "ocid1.tenancy.oc1..root"
REGION = "me-jeddah-1"
READ_PREFIXES = ("list_", "get_", "search_")


def ns(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


class Resp:
    def __init__(self, data: Any, next_page: str | None = None) -> None:
        self.data = data
        self.next_page = next_page


def service_error(status: int, code: str = "NotAuthorizedOrNotFound", message: str = "denied"):
    return oci.exceptions.ServiceError(status, code, {}, message)


class FakeClient:
    """Each handler is a value, an Exception to raise, or a callable(*args, **kwargs)."""

    def __init__(self, **handlers: Any) -> None:
        self._handlers = handlers
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str) -> Callable[..., Resp]:
        if name.startswith("_"):
            raise AttributeError(name)
        assert name.startswith(READ_PREFIXES), f"collector called non-read operation {name}"
        if name not in self._handlers:
            raise AssertionError(f"unexpected SDK call {name}")

        def call(*args: Any, **kwargs: Any) -> Resp:
            self.calls.append((name, args, kwargs))
            handler = self._handlers[name]
            result = handler(*args, **kwargs) if callable(handler) else handler
            if isinstance(result, BaseException):
                raise result
            return result if isinstance(result, Resp) else Resp(result)

        return call


def by_compartment(mapping: dict[str, Any], default: Any = None) -> Callable[..., Any]:
    """Handler that answers per compartment_id (keyword or last positional arg)."""
    def handler(*args: Any, **kwargs: Any) -> Any:
        comp = kwargs.get("compartment_id", args[-1] if args else None)
        return mapping.get(comp, [] if default is None else default)
    return handler


class FakeAuth:
    kind = "api_key"
    tenancy_id = TENANCY
    region = REGION
    profile = "DEFAULT"
    token_expires_at = None

    def reload(self) -> None:
        pass

    def token_expired(self) -> bool:
        return False


def search_item(rtype: str, identifier: str, compartment: str, name: str = "x", state: str = "Active"):
    return ns(resource_type=rtype, identifier=identifier, compartment_id=compartment, display_name=name,
              lifecycle_state=state, time_created=None, availability_domain=None)


@pytest.fixture
def make_ctx():
    """Build a RegionContext for one collector over fake clients.

    make_ctx(collector, {ClientClass: FakeClient}, search={"Instance": [...]}, compartments=[...])
    Returns (ctx, run) — run.errors gives recorded ErrorRecords.
    """
    pools: list[ThreadPoolExecutor] = []

    def factory(collector, clients: dict[type, FakeClient], search: dict[str, list] | None = None,
                compartments: list[str] | None = None, ads: list[str] | None = None,
                namespace: str = "ns1"):
        def client_factory(cls, region):
            if cls not in clients:
                raise AssertionError(f"collector used unexpected client {cls.__name__}")
            return clients[cls]

        run = Collection(FakeAuth(), [REGION], client_factory=client_factory, collectors=[collector])
        comps = compartments or [TENANCY]
        run.compartments = {c: Compartment(c, c.rsplit(".", 1)[-1], None if c == TENANCY else TENANCY)
                            for c in [TENANCY, *comps]}
        if search is not None:
            run._search[REGION] = search
        # search=None means Search failed for the region -> collectors scan all compartments.
        run._ads[REGION] = ads if ads is not None else ["xyz:ME-JEDDAH-1-AD-1"]
        run._namespace = namespace
        pool = ThreadPoolExecutor(4)
        pools.append(pool)
        run.io_pool = pool
        ctx = RegionContext(run, REGION, collector)
        run.errors = lambda: list(run._errors.values())
        return ctx, run

    yield factory
    for p in pools:
        p.shutdown(wait=True)
