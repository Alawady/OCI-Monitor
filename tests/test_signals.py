"""Monitoring metric signals and alarms."""

from datetime import datetime, timedelta, timezone

import oci

from ocimon import metrics
from ocimon.collectors.alarms import AlarmCollector, alarm_state
from ocimon.collectors.compute import ComputeCollector
from ocimon.demo import build_demo
from ocimon.engine import Collection
from ocimon.filters import ResourceQuery, filter_resources
from ocimon.models import Resource

from .conftest import TENANCY, FakeAuth, FakeClient, Resp, by_compartment, ns, search_item, service_error

A = "ocid1.compartment.oc1..a"
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def res(type_="instance", state="RUNNING", health=None, rid="ocid1.instance.oc1.x.AbC"):
    return Resource(id=rid, name="n", type=type_, region="r", compartment_id=A, state=state, health=health)


def series(rid, *values, **dims):
    points = [ns(timestamp=NOW - timedelta(minutes=5 * (len(values) - i)), value=v) for i, v in enumerate(values)]
    return ns(dimensions={"resourceId": rid, **dims}, aggregated_datapoints=points)


# --- thresholds & health ----------------------------------------------------------

def test_evaluate_thresholds():
    assert metrics.evaluate({}) == (None, [])
    assert metrics.evaluate({"cpu_pct": 50, "memory_pct": 60, "fs_status": 0}) == ("OK", [])
    level, reasons = metrics.evaluate({"memory_pct": 93.6})
    assert level == "WARNING" and reasons == ["Memory 93.6% (≥ 90%)"]
    assert metrics.evaluate({"memory_pct": 97.0})[0] == "CRITICAL"
    assert metrics.evaluate({"storage_pct": 79.9})[0] == "OK"
    assert metrics.evaluate({"storage_pct": 80.2})[0] == "WARNING"  # e.g. a MySQL volume "almost full"
    assert metrics.evaluate({"storage_pct": 90})[0] == "CRITICAL"
    assert metrics.evaluate({"cpu_pct": 99})[0] == "WARNING"  # CPU never goes critical on its own
    assert metrics.evaluate({"fs_status": 1})[0] == "CRITICAL"


def test_apply_never_hides_worse_health():
    lb = res("load_balancer", "ACTIVE", health="CRITICAL")
    metrics.apply(lb, {"cpu_pct": 10})
    assert lb.health == "CRITICAL"
    inst = res()
    metrics.apply(inst, {"memory_pct": 95})
    assert inst.health == "WARNING" and inst.problem == "warning" and inst.health_reasons


def test_latest_by_resource_uses_newest_point_and_max_across_series():
    data = [series("OCID1.A", 10, 70), series("ocid1.a", 5, 30, volumeType="block"), series("ocid1.b"),
            ns(dimensions={}, aggregated_datapoints=[ns(timestamp=NOW, value=1)])]
    assert metrics.latest_by_resource(data) == {"ocid1.a": 70}


def test_query_tasks_subtree_from_tenancy_else_per_compartment():
    items = [res(), res("mysql_db_system", "ACTIVE"), res("instance", "STOPPED"),
             Resource(id="i2", name="n", type="instance", region="r", compartment_id="B", state="RUNNING")]
    from_tenancy = metrics.query_tasks(items, TENANCY, TENANCY, lambda: None)
    assert {(s.namespace, c) for s, c, _ in from_tenancy} == {("oci_computeagent", TENANCY),
                                                             ("oci_compute_instance_health", TENANCY),
                                                             ("oci_mysql_database", TENANCY)}
    assert len(from_tenancy) == 6  # cpu, memory, fs + mysql storage, cpu, memory; no ADB
    sub = metrics.query_tasks(items, A, TENANCY, lambda: None, [metrics.SPECS[0]])
    assert sorted(c for _, c, _ in sub) == sorted(["B", A])  # no subtree below the tenancy


# --- engine integration -----------------------------------------------------------

def test_engine_enriches_rows_and_records_metric_errors():
    def summarize(compartment, details, compartment_id_in_subtree=False):
        assert compartment == TENANCY and compartment_id_in_subtree
        if details.namespace == "oci_compute_instance_health":
            return service_error(404)
        value = {"CpuUtilization[5m].mean()": 12.0, "MemoryUtilization[5m].mean()": 93.6}[details.query]
        return Resp([series("OCID1.INSTANCE.OC1.X.WEB", value)])

    inst = ns(id="ocid1.instance.oc1.x.web", display_name="web", compartment_id=A, lifecycle_state="RUNNING",
              shape="VM", shape_config=None, availability_domain=None, fault_domain=None, time_created=None)
    clients = {
        oci.identity.IdentityClient: FakeClient(get_tenancy=lambda t: ns(name="acme"), list_compartments=lambda p, **k: [
            ns(id=A, name="prod", compartment_id=TENANCY, lifecycle_state="ACTIVE", description="")]),
        oci.resource_search.ResourceSearchClient: FakeClient(
            search_resources=Resp(ns(items=[search_item("Instance", inst.id, A)]))),
        oci.core.ComputeClient: FakeClient(list_instances=by_compartment({A: [inst]}), list_vnic_attachments=[]),
        oci.core.VirtualNetworkClient: FakeClient(),
        oci.monitoring.MonitoringClient: FakeClient(summarize_metrics_data=summarize),
    }
    snap = Collection(FakeAuth(), ["me-jeddah-1"], workers=4, collectors=[ComputeCollector()],
                      client_factory=lambda cls, region: clients[cls]).run()
    (r,) = snap.resources
    assert r.metrics == {"cpu_pct": 12.0, "memory_pct": 93.6}
    assert r.health == "WARNING" and r.problem == "warning"
    assert [(e.service, e.operation, e.kind) for e in snap.errors] == [
        ("Monitoring", "SummarizeMetricsData:oci_compute_instance_health", "permission")]


# --- alarms -------------------------------------------------------------------------

def alarm_def(id_, comp=A, enabled=True, suppression=None, severity="CRITICAL"):
    return ns(id=id_, display_name=id_, compartment_id=comp, is_enabled=enabled, suppression=suppression,
              severity=severity, namespace="oci_computeagent", query="CpuUtilization[1m].mean() > 90",
              alarm_summary=None, destinations=["topic"], metric_compartment_id=comp)


def alarm_status(id_, status="OK", severity="CRITICAL", triggered=None):
    return ns(id=id_, status=status, severity=severity, timestamp_triggered=triggered, suppression=None,
              alarm_summary=f"{id_} is {status}")


def test_alarm_state():
    future = ns(time_suppress_from=NOW - timedelta(hours=1), time_suppress_until=NOW + timedelta(hours=1))
    past = ns(time_suppress_from=NOW - timedelta(hours=3), time_suppress_until=NOW - timedelta(hours=1))
    assert alarm_state(alarm_def("a", enabled=False), alarm_status("a", "FIRING"), NOW) == "DISABLED"
    assert alarm_state(alarm_def("a", suppression=future), alarm_status("a", "FIRING"), NOW) == "SUPPRESSED"
    assert alarm_state(alarm_def("a", suppression=past), alarm_status("a", "FIRING"), NOW) == "FIRING"
    assert alarm_state(alarm_def("a"), None, NOW) == "UNKNOWN"


def test_alarm_collector(make_ctx):
    calls = []

    def list_alarms(comp, **kw):
        calls.append(("defs", comp, kw))
        return [alarm_def("fire"), alarm_def("calm", severity="WARNING"), alarm_def("off", enabled=False),
                alarm_def("elsewhere", comp="ocid1.compartment.oc1..unknown")]

    def list_status(comp, **kw):
        calls.append(("status", comp, kw))
        return [alarm_status("fire", "FIRING", "CRITICAL", NOW), alarm_status("calm", "OK", "WARNING", NOW),
                alarm_status("off", "FIRING")]

    client = FakeClient(list_alarms=list_alarms, list_alarms_status=list_status)
    ctx, run = make_ctx(AlarmCollector(), {oci.monitoring.MonitoringClient: client},
                        search={"Alarm": [search_item("Alarm", "fire", A)]}, compartments=[A])
    out = {r.id: r for r in AlarmCollector().collect(ctx)}
    assert set(out) == {"fire", "calm", "off"}  # alarms outside the monitored tree are skipped
    assert out["fire"].state == "FIRING" and out["fire"].health == "CRITICAL" and out["fire"].problem == "critical"
    assert out["fire"].details["triggered_at"] == NOW.isoformat() and out["fire"].details["notifications"] == 1
    assert out["calm"].state == "OK" and out["calm"].problem is None
    assert out["off"].state == "DISABLED" and out["off"].problem is None
    # Tenancy root: one subtree call each
    assert all(c[1] == TENANCY and c[2]["compartment_id_in_subtree"] for c in calls) and len(calls) == 2


def test_alarms_only_on_request():
    snap = build_demo(0)
    default = filter_resources(snap, ResourceQuery.from_params())
    assert not any(r.type == "alarm" for r in default)
    alarms = filter_resources(snap, ResourceQuery.from_params(type="alarm"))
    assert alarms and all(r.type == "alarm" for r in alarms)
    firing = [r for r in alarms if r.state == "FIRING"]
    assert {r.problem for r in firing} == {"critical", "warning"}
