"""Cost data: Usage API source, aggregation views, and the API endpoints."""

from datetime import date, datetime, timedelta, timezone

import oci
import pytest
from fastapi.testclient import TestClient

from ocimon.app import create_app
from ocimon.cost import CostReport, CostRow, DemoCostSource, LiveCostSource, ResourceCostRow, cost_view
from ocimon.demo import DemoSource, build_demo
from ocimon.filters import resource_view
from ocimon.models import Resource
from ocimon.refresher import Refresher
from ocimon.settings import Settings

from .conftest import FakeAuth, FakeClient, Resp, ns, service_error

TODAY = date(2026, 11, 2)


@pytest.fixture(scope="module")
def demo():
    snap = build_demo(0)
    return snap, DemoCostSource(lambda: snap, delay=0)()


# --- live source ----------------------------------------------------------------

def usage(day, amount, currency="USD", **kw):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return ns(time_usage_started=start, computed_amount=amount, currency=currency,
              service=kw.get("service", "Compute"), compartment_id=kw.get("compartment", "c1"),
              region=kw.get("region", "me-jeddah-1"), resource_id=kw.get("resource_id"))


def test_live_source_queries_home_region_and_normalizes():
    calls = []
    today = datetime.now(timezone.utc).date()

    def summarize(details, **kw):
        calls.append(details)
        if "resourceId" in details.group_by:
            return Resp(ns(items=[usage(today, 5.0, resource_id="OCID1.INSTANCE.OC1.ABC"),
                                  usage(today, 0.0, resource_id="ocid1.vnic.oc1.x")]))
        return Resp(ns(items=[usage(today, 10.0), usage(today, 0.0, currency=" "),
                              usage(today - timedelta(days=1), 2.5, service="Block Storage")]))

    regions_used = []
    usage_client = FakeClient(request_summarized_usages=summarize)
    identity = FakeClient(list_region_subscriptions=lambda t: [
        ns(region_name="me-riyadh-1", is_home_region=False), ns(region_name="me-jeddah-1", is_home_region=True)])

    def factory(cls, region):
        regions_used.append((cls.__name__, region))
        return usage_client if cls is oci.usage_api.UsageapiClient else identity

    report = LiveCostSource(FakeAuth(), client_factory=factory)()
    assert ("UsageapiClient", "me-jeddah-1") in regions_used
    service_q, resource_q = calls
    assert service_q.granularity == "DAILY" and service_q.query_type == "COST"
    assert service_q.group_by == ["service", "compartmentId", "region"]
    assert resource_q.group_by == ["resourceId", "service", "compartmentId", "region"]
    assert (service_q.time_usage_ended - service_q.time_usage_started).days == 60
    assert (resource_q.time_usage_ended - resource_q.time_usage_started).days == 31
    assert service_q.time_usage_started.hour == 0  # Usage API needs day-aligned times
    assert report.currency == "USD" and report.error is None
    assert len(report.rows) == 2  # zero-amount rows dropped
    assert [r.resource_id for r in report.resource_rows] == ["ocid1.instance.oc1.abc"]  # lower-cased


def test_live_source_reports_missing_permission():
    usage_client = FakeClient(request_summarized_usages=service_error(404, "NotAuthorizedOrNotFound"))
    identity = FakeClient(list_region_subscriptions=lambda t: [ns(region_name="me-jeddah-1", is_home_region=True)])
    report = LiveCostSource(FakeAuth(), client_factory=lambda cls, r: usage_client if "Usage" in cls.__name__ else identity)()
    assert report.error_kind == "permission" and "usage-reports" in report.error
    assert report.rows == []


def test_live_source_raises_other_errors():
    usage_client = FakeClient(request_summarized_usages=service_error(500, "InternalServerError"))
    identity = FakeClient(list_region_subscriptions=lambda t: [ns(region_name="me-jeddah-1", is_home_region=True)])
    with pytest.raises(oci.exceptions.ServiceError):
        LiveCostSource(FakeAuth(), client_factory=lambda cls, r: usage_client if "Usage" in cls.__name__ else identity)()


# --- views ----------------------------------------------------------------------

def tiny_report():
    rows, res = [], []
    for back in range(15):
        day = TODAY - timedelta(days=back)
        rows.append(CostRow(day, "Compute", "c1", "r1", 10.0 if back <= 7 else 5.0))
        rows.append(CostRow(day, "Block Storage", "c2", "r2", 1.0))
        res.append(ResourceCostRow(day, "ocid1.instance.oc1.r1.a", "Compute", "c1", "r1", 10.0 if back <= 7 else 5.0))
    return CostReport(rows, res, "USD", TODAY)


def test_view_totals_and_comparison():
    v = cost_view(tiny_report(), None, 7)
    t = v["totals"]
    assert t["period"] == 77.0  # 7 full days x (10 + 1), today excluded
    assert t["previous"] == 7 * 5 + 7 * 1  # days 8..14 back
    assert t["delta_pct"] == round((77 - 42) / 42 * 100, 1)
    assert t["daily_avg"] == 11.0 and t["yesterday"] == 11.0 and t["today_partial"] == 11.0
    assert len(v["chart"]) == 8 and v["chart"][-1]["partial"] and not v["chart"][0]["partial"]
    assert sum(s["amount"] for s in v["by_service"]) == pytest.approx(t["period"])
    assert v["by_service"][0]["service"] == "Compute"


def test_view_region_filter():
    v = cost_view(tiny_report(), None, 7, region="r2")
    assert v["totals"]["period"] == 7.0
    assert [s["service"] for s in v["by_service"]] == ["Block Storage"]
    assert v["top_resources"] == []  # the only resource is in r1


def test_demo_view_compartment_filter_and_stable_colors(demo):
    snap, report = demo
    everything = cost_view(report, snap, 7)
    prod = next(c.id for c in snap.compartments.values() if c.name == "Production")
    scoped = cost_view(report, snap, 7, compartment=prod)
    assert 0 < scoped["totals"]["period"] < everything["totals"]["period"]
    # Colors follow the service, not its rank under the current filter.
    slots_all = {s["name"]: s["slot"] for s in everything["series"]}
    for s in scoped["series"]:
        assert slots_all.get(s["name"], s["slot"]) == s["slot"]
    # Compartment breakdown is the scope's children (rolled up) and adds up.
    names = {c["name"] for c in scoped["by_compartment"]}
    assert names <= {"prod-app", "prod-data", "Production (directly)"}
    assert sum(c["amount"] for c in scoped["by_compartment"]) == pytest.approx(scoped["totals"]["period"], abs=0.05)


def test_demo_view_top_resources(demo):
    snap, report = demo
    v = cost_view(report, snap, 14)
    top = v["top_resources"]
    assert len(top) == 10 and [t["amount"] for t in top] == sorted((t["amount"] for t in top), reverse=True)
    assert all(len(t["daily"]) == 14 for t in top)
    assert any(t["on_dashboard"] for t in top)
    assert 0 < v["totals"]["covered_pct"] < 100  # backups etc. aren't on the dashboard
    assert len(v["chart"]) == 15


def test_resource_cost_lookup(demo):
    snap, report = demo
    inst = next(r for r in snap.resources if r.name == "prod-web-01")
    cost = report.for_resource(inst)
    assert cost and len(cost["daily"]) == 7 and cost["avg_day"] > 0
    assert resource_view(snap, inst, report)["cost"] == cost
    assert resource_view(snap, inst, None)["cost"] is None


# --- API ------------------------------------------------------------------------

def client_with(cost_source=None, refresh_cost=True):
    inv = Refresher(DemoSource(delay=0), 120, mode="demo")
    inv.refresh_once()
    cost = Refresher(cost_source(inv), 3600, mode="demo", name="cost") if cost_source else None
    if cost and refresh_cost:
        cost.refresh_once()
    app = create_app(Settings(), inv, {"kind": "demo"}, cost)
    return TestClient(app, base_url="http://127.0.0.1:8080"), cost


def test_cost_endpoint_ok_and_resources_carry_cost():
    c, _ = client_with(lambda inv: DemoCostSource(lambda: inv.snapshot, delay=0))
    d = c.get("/api/cost", params={"days": 14}).json()
    assert d["status"] == "ok" and d["days"] == 14 and d["currency"] == "USD" and d["meta"]["status"] == "ok"
    assert c.get("/api/cost", params={"days": 99}).json()["days"] == 7  # unsupported range falls back
    res = c.get("/api/resources", params={"type": "instance"}).json()
    assert res["currency"] == "USD" and any(i["cost"] for i in res["items"])
    assert c.get("/api/meta").json()["cost_enabled"] is True
    assert c.get("/api/health").json()["cost"]["status"] == "ok"
    r = c.post("/api/refresh", params={"target": "cost"}, headers={"X-OCIMON": "1"})
    assert r.status_code == 202 and r.json()["target"] == "cost"


def test_cost_endpoint_loading_and_disabled():
    c, _ = client_with(lambda inv: DemoCostSource(lambda: inv.snapshot, delay=0), refresh_cost=False)
    assert c.get("/api/cost").json()["status"] == "loading"
    assert c.get("/api/resources").json()["currency"] is None

    c, _ = client_with(None)
    assert c.get("/api/cost").json()["status"] == "disabled"
    assert c.get("/api/meta").json()["cost_enabled"] is False
    assert c.post("/api/refresh", params={"target": "cost"}, headers={"X-OCIMON": "1"}).status_code == 409


def test_cost_endpoint_permission_error():
    denied = CostReport([], [], "", TODAY, error="No permission to read cost data", error_kind="permission")
    c, _ = client_with(lambda inv: (lambda: denied))
    d = c.get("/api/cost").json()
    assert d["status"] == "error" and d["meta"]["error_kind"] == "permission"
    assert c.get("/api/resources").json()["currency"] is None  # no cost column without data


def test_buckets_match_by_namespace_and_name():
    """Object Storage bills buckets as "namespace/name" (sometimes just "name"), not by OCID."""
    bucket = Resource(id="ocid1.bucket.oc1.r1.xyz", name="backups", type="bucket", region="r1",
                      compartment_id="c1", refs={"namespace": "ns1", "bucket": "backups"})
    other_region = Resource(id="ocid1.bucket.oc1.r2.abc", name="backups", type="bucket", region="r2",
                            compartment_id="c1", refs={"namespace": "ns1", "bucket": "backups"})
    res = []
    for back in range(1, 8):
        day = TODAY - timedelta(days=back)
        res += [ResourceCostRow(day, "ns1/backups", "Object Storage", "c1", "r1", 2.0),
                ResourceCostRow(day, "backups", "Object Storage", "c1", "r1", 0.5),  # same bucket, bare name
                ResourceCostRow(day, "ns1/backups", "Object Storage", "c1", "r2", 9.0)]  # same name, other region
    report = CostReport([], res, "USD", TODAY)
    assert report.for_resource(bucket)["avg_day"] == 2.5
    assert report.for_resource(other_region)["avg_day"] == 9.0

    snap = build_demo(0)
    snap.resources = [bucket, other_region]
    snap.__dict__.pop("by_id", None)
    top = cost_view(report, snap, 7)["top_resources"]
    assert [(t["id"], t["amount"], t["on_dashboard"]) for t in top] == [
        ("ocid1.bucket.oc1.r2.abc", 63.0, True), ("ocid1.bucket.oc1.r1.xyz", 17.5, True)]
