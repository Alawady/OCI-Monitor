"""A full refresh through Collection.run() with fake clients."""

import oci

from ocimon.collectors.compute import ComputeCollector
from ocimon.collectors.object_storage import ObjectStorageCollector
from ocimon.engine import Collection, LiveSource

from .conftest import TENANCY, FakeAuth, FakeClient, Resp, by_compartment, ns, search_item, service_error

A = "ocid1.compartment.oc1..a"
B = "ocid1.compartment.oc1..b"
OUTSIDE = "ocid1.compartment.oc1..not-in-tree"


def identity_client(**extra):
    return FakeClient(
        get_tenancy=lambda t: ns(name="acme"),
        list_compartments=lambda parent, **kw: [
            ns(id=A, name="prod", compartment_id=TENANCY, lifecycle_state="ACTIVE", description=""),
            ns(id=B, name="app", compartment_id=A, lifecycle_state="ACTIVE", description="apps"),
        ],
        list_availability_domains=lambda t: [ns(name="xyz:ME-JEDDAH-1-AD-1")],
        **extra,
    )


def instance(id_, comp):
    return ns(id=id_, display_name=id_, compartment_id=comp, lifecycle_state="RUNNING", shape="VM",
              shape_config=None, availability_domain=None, fault_domain=None, time_created=None)


def make(clients, collectors, regions=("me-jeddah-1",), root=None):
    return Collection(FakeAuth(), list(regions), root, workers=4, collectors=collectors,
                      client_factory=lambda cls, region: clients[cls])


def test_full_run_with_search_fallback_and_errors():
    queries = []

    def search(details, **kw):
        queries.append(details.query)
        return Resp(ns(items=[
            search_item("Instance", "i-a", A, "web"),
            search_item("Instance", "i-b", B, "hidden"),      # B's listing is denied -> fallback row
            search_item("Instance", "i-x", OUTSIDE, "other"),  # not in our tree -> ignored
        ]))

    clients = {
        oci.identity.IdentityClient: identity_client(),
        oci.resource_search.ResourceSearchClient: FakeClient(search_resources=search),
        oci.core.ComputeClient: FakeClient(
            list_instances=by_compartment({A: [instance("i-a", A)], B: service_error(404)}),
            list_vnic_attachments=by_compartment({A: [], B: []}),
        ),
        oci.core.VirtualNetworkClient: FakeClient(),
    }
    snap = make(clients, [ComputeCollector()]).run()

    assert snap.tenancy_name == "acme"
    assert set(snap.compartments) == {TENANCY, A, B}
    assert snap.paths[B] == "prod/app"
    assert queries == ["query Instance resources"]
    by_id = {r.id: r for r in snap.resources}
    assert set(by_id) == {"i-a", "i-b"}
    assert by_id["i-a"].source == "api"
    assert by_id["i-b"].source == "search" and by_id["i-b"].state == "ACTIVE"  # normalized from "Active"
    assert [(e.operation, e.compartment_id, e.kind) for e in snap.errors] == [("ListInstances", B, "permission")]
    assert snap.stats["search_fallbacks"] == 1


def test_search_400_retries_per_type():
    seen = []

    def search(details, **kw):
        seen.append(details.query)
        if "," in details.query:
            return service_error(400, "InvalidParameter", "Unknown resource type")
        if "Bucket" in details.query:
            return service_error(400, "InvalidParameter", "Unknown resource type 'bucket'")
        return Resp(ns(items=[search_item("Instance", "i-a", A)]))

    clients = {
        oci.identity.IdentityClient: identity_client(),
        oci.resource_search.ResourceSearchClient: FakeClient(search_resources=search),
        oci.core.ComputeClient: FakeClient(list_instances=by_compartment({A: [instance("i-a", A)]}),
                                           list_vnic_attachments=[]),
        oci.core.VirtualNetworkClient: FakeClient(),
        oci.object_storage.ObjectStorageClient: FakeClient(get_namespace="ns"),
    }
    snap = make(clients, [ComputeCollector(), ObjectStorageCollector()]).run()
    assert seen[0] == "query Bucket, Instance resources"
    assert set(seen[1:]) == {"query Bucket resources", "query Instance resources"}
    assert [r.id for r in snap.resources] == ["i-a"]
    assert [e.operation for e in snap.errors] == ["SearchResources:Bucket"]


def test_search_unavailable_scans_all_compartments():
    compute = FakeClient(list_instances=by_compartment({B: [instance("i-b", B)]}), list_vnic_attachments=[])
    clients = {
        oci.identity.IdentityClient: identity_client(),
        oci.resource_search.ResourceSearchClient: FakeClient(search_resources=service_error(403, "NotAuthorized")),
        oci.core.ComputeClient: compute,
        oci.core.VirtualNetworkClient: FakeClient(),
    }
    snap = make(clients, [ComputeCollector()]).run()
    listed = {c[2]["compartment_id"] for c in compute.calls if c[0] == "list_instances"}
    assert listed == {TENANCY, A, B}
    assert [r.id for r in snap.resources] == ["i-b"]
    assert snap.errors[0].service == "Resource Search"


def test_compartment_listing_denied_keeps_root():
    clients = {
        oci.identity.IdentityClient: FakeClient(get_tenancy=lambda t: ns(name="acme"),
                                                list_compartments=service_error(404)),
        oci.resource_search.ResourceSearchClient: FakeClient(search_resources=Resp(ns(items=[]))),
    }
    snap = make(clients, [ComputeCollector()]).run()
    assert list(snap.compartments) == [TENANCY]
    assert snap.errors[0].operation == "ListCompartments"


def test_crashing_collector_does_not_break_refresh():
    class Boom(ComputeCollector):
        key, label = "boom", "Boom"

        def collect(self, ctx):
            raise RuntimeError("bug")

    clients = {
        oci.identity.IdentityClient: identity_client(),
        oci.resource_search.ResourceSearchClient: FakeClient(search_resources=Resp(ns(items=[]))),
        oci.core.ComputeClient: FakeClient(list_instances=[], list_vnic_attachments=[]),
        oci.core.VirtualNetworkClient: FakeClient(),
    }
    snap = make(clients, [Boom(), ComputeCollector()]).run()
    assert [(e.service, e.operation) for e in snap.errors] == [("Boom", "collect")]


def test_stop_cancels_run_quietly(caplog):
    import threading

    import pytest

    from ocimon.engine import RefreshCancelled

    stop = threading.Event()

    def list_instances(compartment_id):
        stop.set()  # shutdown starts while collectors are running
        return [instance("i-a", A)]

    compute = FakeClient(list_instances=list_instances, list_vnic_attachments=[])
    clients = {
        oci.identity.IdentityClient: identity_client(),
        oci.resource_search.ResourceSearchClient: FakeClient(
            search_resources=Resp(ns(items=[search_item("Instance", "i-a", A)]))),
        oci.core.ComputeClient: compute,
        oci.core.VirtualNetworkClient: FakeClient(get_vnic=lambda v: None),
    }
    run = Collection(FakeAuth(), ["me-jeddah-1"], workers=4, collectors=[ComputeCollector()],
                     client_factory=lambda cls, region: clients[cls], shutdown_event=stop)
    with pytest.raises(RefreshCancelled):
        run.run()
    assert run._errors == {}
    assert "failed" not in caplog.text


def test_sub_compartment_root_walks_tree():
    tree = {A: [ns(id=B, name="app", compartment_id=A, lifecycle_state="ACTIVE", description="")], B: []}
    calls = []

    def list_compartments(parent, **kw):
        calls.append(kw)
        return tree[parent]

    clients = {
        oci.identity.IdentityClient: FakeClient(get_compartment=lambda c: ns(name="prod"),
                                                list_compartments=list_compartments),
        oci.resource_search.ResourceSearchClient: FakeClient(search_resources=Resp(ns(items=[]))),
    }
    snap = make(clients, [], root=A).run()
    assert snap.root_id == A and set(snap.compartments) == {A, B}
    assert all("compartment_id_in_subtree" not in kw for kw in calls)


def test_live_source_resolves_all_regions():
    subs = [ns(region_name="me-riyadh-1", status="READY", is_home_region=False),
            ns(region_name="me-jeddah-1", status="READY", is_home_region=True),
            ns(region_name="eu-paris-1", status="IN_PROGRESS", is_home_region=False)]
    identity = FakeClient(list_region_subscriptions=lambda t: subs)
    src = LiveSource(FakeAuth(), "all", client_factory=lambda cls, region: identity)
    assert src.regions() == ["me-jeddah-1", "me-riyadh-1"]
    assert LiveSource(FakeAuth(), "").regions() == ["me-jeddah-1"]
    assert LiveSource(FakeAuth(), "us-phoenix-1, me-dubai-1").regions() == ["us-phoenix-1", "me-dubai-1"]
