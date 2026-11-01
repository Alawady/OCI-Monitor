"""Collectors against mocked SDK responses."""

from datetime import datetime, timezone

import oci

from ocimon.collectors.api_gateway import ApiGatewayCollector
from ocimon.collectors.block_storage import BlockStorageCollector
from ocimon.collectors.compute import ComputeCollector
from ocimon.collectors.database import AutonomousDatabaseCollector, DbSystemCollector, MysqlCollector
from ocimon.collectors.functions import FunctionsCollector
from ocimon.collectors.load_balancer import LoadBalancerCollector, NetworkLoadBalancerCollector
from ocimon.collectors.network import NetworkCollector
from ocimon.collectors.object_storage import ObjectStorageCollector
from ocimon.collectors.oke import OkeCollector

from .conftest import FakeClient, Resp, by_compartment, ns, search_item, service_error

A = "ocid1.compartment.oc1..a"
B = "ocid1.compartment.oc1..b"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
AD = "xyz:ME-JEDDAH-1-AD-1"


def instance(id_, comp, state="RUNNING", name=None):
    return ns(id=id_, display_name=name or id_, compartment_id=comp, lifecycle_state=state, shape="VM.Standard.E5.Flex",
              shape_config=ns(ocpus=2.0, memory_in_gbs=16.0), availability_domain=AD,
              fault_domain="FAULT-DOMAIN-1", time_created=NOW)


# --- compute ------------------------------------------------------------------

def test_compute_instances_with_ips(make_ctx):
    compute = FakeClient(
        list_instances=by_compartment({A: [instance("i1", A), instance("i2", A, "TERMINATED")]}),
        list_vnic_attachments=by_compartment({A: [
            ns(instance_id="i1", vnic_id="v1", lifecycle_state="ATTACHED"),
            ns(instance_id="i1", vnic_id="v2", lifecycle_state="ATTACHED"),
            ns(instance_id="i2", vnic_id="v3", lifecycle_state="ATTACHED"),
        ]}),
    )
    vnics = {
        "v1": ns(private_ip="10.0.0.2", public_ip="129.1.1.1", is_primary=False, hostname_label=None),
        "v2": ns(private_ip="10.0.0.3", public_ip=None, is_primary=True, hostname_label="web1"),
    }
    vn = FakeClient(get_vnic=lambda v: vnics[v])
    ctx, run = make_ctx(ComputeCollector(), {oci.core.ComputeClient: compute, oci.core.VirtualNetworkClient: vn},
                        search={"Instance": [search_item("Instance", "i1", A)]}, compartments=[A, B])
    out = {r.id: r for r in ComputeCollector().collect(ctx)}

    i1 = out["i1"]
    assert i1.state == "RUNNING" and i1.region == "me-jeddah-1"
    assert i1.details["ocpus"] == 2.0 and i1.details["memory_gb"] == 16.0
    assert i1.details["availability_domain"] == "AD-1"
    assert i1.details["private_ips"] == ["10.0.0.3", "10.0.0.2"]  # primary VNIC first
    assert i1.details["public_ips"] == ["129.1.1.1"]
    assert i1.details["hostname"] == "web1"
    # Terminated instance: no VNIC lookups
    assert out["i2"].state == "TERMINATED"
    assert all(c[1] != ("v3",) for c in vn.calls)
    # Only the compartment where Search found instances was listed
    assert {c[2]["compartment_id"] for c in compute.calls} == {A}
    assert run.errors() == []


def test_permission_error_is_recorded_and_others_continue(make_ctx):
    compute = FakeClient(
        list_instances=by_compartment({A: service_error(404), B: [instance("ib", B)]}),
        list_vnic_attachments=by_compartment({A: service_error(404), B: []}),
    )
    ctx, run = make_ctx(ComputeCollector(), {oci.core.ComputeClient: compute,
                                             oci.core.VirtualNetworkClient: FakeClient()},
                        search=None, compartments=[A, B])  # Search failed -> scan everything
    out = ComputeCollector().collect(ctx)
    assert [r.id for r in out] == ["ib"]
    errors = run.errors()
    assert {e.operation for e in errors} == {"ListInstances", "ListVnicAttachments"}
    assert all(e.kind == "permission" and e.compartment_id == A and e.status == 404 for e in errors)


def test_get_vnic_failure_still_returns_instance(make_ctx):
    compute = FakeClient(
        list_instances=by_compartment({A: [instance("i1", A)]}),
        list_vnic_attachments=by_compartment({A: [ns(instance_id="i1", vnic_id="v1", lifecycle_state="ATTACHED")]}),
    )
    vn = FakeClient(get_vnic=service_error(500, "InternalError", "boom"))
    ctx, run = make_ctx(ComputeCollector(), {oci.core.ComputeClient: compute, oci.core.VirtualNetworkClient: vn},
                        search={"Instance": [search_item("Instance", "i1", A)]}, compartments=[A])
    out = ComputeCollector().collect(ctx)
    assert out[0].details["private_ips"] == []
    assert run.errors()[0].kind == "error" and run.errors()[0].operation == "GetVnic"


# --- block storage --------------------------------------------------------------

def _volume(id_, comp, ad=AD):
    return ns(id=id_, display_name=id_, compartment_id=comp, lifecycle_state="AVAILABLE", size_in_gbs=100,
              vpus_per_gb=10, availability_domain=ad, time_created=NOW)


def test_block_storage_attachments_across_compartments(make_ctx):
    bs = FakeClient(
        list_volumes=by_compartment({A: [_volume("vol1", A), _volume("vol2", A)]}),
        list_boot_volumes=by_compartment({A: [_volume("boot1", A)]}),
    )
    compute = FakeClient(
        # vol1 is attached to an instance in compartment B
        list_volume_attachments=by_compartment({B: [ns(volume_id="vol1", instance_id="ib", lifecycle_state="ATTACHED")],
                                                A: [ns(volume_id="vol2", instance_id="ia", lifecycle_state="DETACHED")]}),
        list_boot_volume_attachments=lambda ad, comp: [ns(boot_volume_id="boot1", instance_id="ia",
                                                          lifecycle_state="ATTACHED")] if comp == A else [],
    )
    search = {"Volume": [search_item("Volume", "vol1", A)], "BootVolume": [search_item("BootVolume", "boot1", A)],
              "Instance": [search_item("Instance", "ib", B)]}
    ctx, run = make_ctx(BlockStorageCollector(), {oci.core.BlockstorageClient: bs, oci.core.ComputeClient: compute},
                        search=search, compartments=[A, B])
    out = {r.id: r for r in BlockStorageCollector().collect(ctx)}
    assert out["vol1"].details["attached"] is True and out["vol1"].refs["instance_id"] == "ib"
    assert out["vol2"].details["attached"] is False
    assert out["boot1"].type == "boot_volume" and out["boot1"].details["attached"] is True
    assert out["vol1"].details["size_gb"] == 100
    assert run.errors() == []


def test_block_storage_unknown_attachment_when_denied(make_ctx):
    bs = FakeClient(list_volumes=by_compartment({A: [_volume("vol1", A)]}))
    compute = FakeClient(list_volume_attachments=by_compartment({A: service_error(403)}))
    ctx, run = make_ctx(BlockStorageCollector(), {oci.core.BlockstorageClient: bs, oci.core.ComputeClient: compute},
                        search={"Volume": [search_item("Volume", "vol1", A)]}, compartments=[A])
    (vol,) = BlockStorageCollector().collect(ctx)
    assert vol.details["attached"] is None  # don't claim "unattached" when we couldn't look
    assert run.errors()[0].operation == "ListVolumeAttachments"


# --- load balancers -------------------------------------------------------------

def test_load_balancer_health_and_backend_sets(make_ctx):
    lbs = [
        ns(id="lb1", display_name="public", compartment_id=A, lifecycle_state="ACTIVE", shape_name="flexible",
           is_private=False, time_created=NOW, listeners={"l": 1},
           ip_addresses=[ns(ip_address="1.2.3.4", is_public=True)],
           backend_sets={"web": ns(backends=[1, 2]), "api": ns(backends=[1])}),
        ns(id="lb2", display_name="new", compartment_id=A, lifecycle_state="CREATING", shape_name="flexible",
           is_private=True, time_created=NOW, listeners={}, ip_addresses=[], backend_sets={}),
    ]
    health = ns(status="CRITICAL", critical_state_backend_set_names=["web"], warning_state_backend_set_names=[],
                unknown_state_backend_set_names=[], total_backend_set_count=2)
    client = FakeClient(list_load_balancers=by_compartment({A: lbs}), get_load_balancer_health=lambda i: health)
    ctx, run = make_ctx(LoadBalancerCollector(), {oci.load_balancer.LoadBalancerClient: client},
                        search={"LoadBalancer": [search_item("LoadBalancer", "lb1", A)]}, compartments=[A])
    out = {r.id: r for r in LoadBalancerCollector().collect(ctx)}
    lb1 = out["lb1"]
    assert lb1.health == "CRITICAL" and lb1.problem == "critical"
    assert lb1.details["backend_sets"] == [{"name": "api", "status": "OK", "backends": 1},
                                           {"name": "web", "status": "CRITICAL", "backends": 2}]
    assert lb1.details["public_ips"] == ["1.2.3.4"]
    # Health isn't fetched for LBs that aren't ACTIVE
    assert out["lb2"].health is None
    assert [c for c in client.calls if c[0] == "get_load_balancer_health"] == [("get_load_balancer_health", ("lb1",), {})]


def test_load_balancer_health_failure_is_unknown(make_ctx):
    lb = ns(id="lb1", display_name="x", compartment_id=A, lifecycle_state="ACTIVE", shape_name="10Mbps",
            is_private=False, time_created=NOW, listeners={}, ip_addresses=[], backend_sets={"bs": ns(backends=[])})
    client = FakeClient(list_load_balancers=by_compartment({A: [lb]}),
                        get_load_balancer_health=service_error(429, "TooManyRequests"))
    ctx, run = make_ctx(LoadBalancerCollector(), {oci.load_balancer.LoadBalancerClient: client},
                        search={"LoadBalancer": [search_item("LoadBalancer", "lb1", A)]}, compartments=[A])
    (r,) = LoadBalancerCollector().collect(ctx)
    assert r.health == "UNKNOWN" and r.details["backend_sets"][0]["status"] == "UNKNOWN"
    assert run.errors()[0].status == 429


def test_network_load_balancer(make_ctx):
    nlb = ns(id="n1", display_name="nlb", compartment_id=A, lifecycle_state="ACTIVE", is_private=True,
             time_created=NOW, listeners={}, ip_addresses=[ns(ip_address="10.0.0.9", is_public=False)],
             backend_sets={"tcp": None})
    health = ns(status="WARNING", critical_state_backend_set_names=[], warning_state_backend_set_names=["tcp"],
                unknown_state_backend_set_names=[], total_backend_set_count=1)
    # list_network_load_balancers returns a *Collection with .items
    client = FakeClient(list_network_load_balancers=by_compartment({A: Resp(ns(items=[nlb]))}),
                        get_network_load_balancer_health=lambda i: health)
    ctx, _ = make_ctx(NetworkLoadBalancerCollector(),
                      {oci.network_load_balancer.NetworkLoadBalancerClient: client},
                      search={"NetworkLoadBalancer": [search_item("NetworkLoadBalancer", "n1", A)]}, compartments=[A])
    (r,) = NetworkLoadBalancerCollector().collect(ctx)
    assert r.health == "WARNING" and r.problem == "warning"
    assert r.details["backend_sets"] == [{"name": "tcp", "status": "WARNING"}]
    assert r.details["private_ips"] == ["10.0.0.9"]


# --- databases ------------------------------------------------------------------

def test_databases(make_ctx):
    db = FakeClient(
        list_db_systems=by_compartment({A: [ns(id="d1", display_name="erp", compartment_id=A, lifecycle_state="AVAILABLE",
                                               time_created=NOW, shape="VM.Standard2.4", version="19.20.0.0",
                                               database_edition="ENTERPRISE_EDITION", cpu_core_count=4, node_count=1,
                                               data_storage_size_in_gbs=256, hostname="erp", availability_domain=AD)]}),
        list_autonomous_databases=by_compartment({A: [ns(id="a1", display_name="adw", compartment_id=A,
                                                         lifecycle_state="AVAILABLE_NEEDS_ATTENTION", time_created=NOW,
                                                         db_workload="DW", db_version="23ai", compute_count=4.0,
                                                         compute_model="ECPU", cpu_core_count=None,
                                                         data_storage_size_in_tbs=1, is_free_tier=False,
                                                         is_dedicated=False, db_name="ADW")]}),
    )
    mysql = FakeClient(list_db_systems=by_compartment({A: [ns(
        id="m1", display_name="orders", compartment_id=A, lifecycle_state="ACTIVE", time_created=NOW,
        mysql_version="8.4.3", shape_name="MySQL.8", is_highly_available=True, availability_domain=AD,
        heat_wave_cluster=ns(cluster_size=2, shape_name="HeatWave.512GB", lifecycle_state="ACTIVE"),
        endpoints=[ns(ip_address="10.0.0.5", port=3306)])]}))
    search = {t: [search_item(t, "x", A)] for t in ("DbSystem", "AutonomousDatabase", "MysqlDbSystem")}
    clients = {oci.database.DatabaseClient: db, oci.mysql.DbSystemClient: mysql}

    ctx, _ = make_ctx(DbSystemCollector(), clients, search=search, compartments=[A])
    (d,) = DbSystemCollector().collect(ctx)
    assert d.details["version"] == "19.20.0.0" and d.state == "AVAILABLE" and d.details["cpu_cores"] == 4

    ctx, _ = make_ctx(AutonomousDatabaseCollector(), clients, search=search, compartments=[A])
    (a,) = AutonomousDatabaseCollector().collect(ctx)
    assert a.details["workload"] == "Data Warehouse" and a.details["compute"] == "4.0 ECPU"
    assert a.problem == "warning"

    ctx, _ = make_ctx(MysqlCollector(), clients, search=search, compartments=[A])
    (m,) = MysqlCollector().collect(ctx)
    assert m.details["heatwave"] == "2 x HeatWave.512GB (ACTIVE)"
    assert m.details["endpoint"] == "10.0.0.5:3306" and m.details["highly_available"] is True


# --- OKE ------------------------------------------------------------------------

def test_oke_clusters_and_node_pool_health(make_ctx):
    cluster = ns(id="c1", name="prod", compartment_id=A, lifecycle_state="ACTIVE", kubernetes_version="v1.31.1",
                 type="ENHANCED_CLUSTER", vcn_id="vcn1", lifecycle_details="", metadata=ns(time_created=NOW),
                 endpoints=ns(public_endpoint=None, private_endpoint="10.0.0.1:6443"))
    pool = lambda id_, comp: ns(id=id_, name=id_, cluster_id="c1", compartment_id=comp, lifecycle_state="ACTIVE",  # noqa: E731
                                kubernetes_version="v1.31.1", node_shape="VM.Standard.E5.Flex",
                                node_shape_config=ns(ocpus=2, memory_in_gbs=16), node_config_details=ns(size=2))
    # p1 has a leftover DELETED node from a scale-down, which must not count against it.
    nodes = {"p1": [ns(lifecycle_state="ACTIVE"), ns(lifecycle_state="ACTIVE"), ns(lifecycle_state="DELETED")],
             "p2": [ns(lifecycle_state="ACTIVE"), ns(lifecycle_state="FAILED")]}
    client = FakeClient(
        list_clusters=by_compartment({A: [cluster]}),
        # p2 lives in another compartment than its cluster
        list_node_pools=by_compartment({A: [pool("p1", A)], B: [pool("p2", B)]}),
        get_node_pool=lambda pid: ns(nodes=nodes[pid]),
    )
    ctx, _ = make_ctx(OkeCollector(), {oci.container_engine.ContainerEngineClient: client},
                      search={"ClustersCluster": [search_item("ClustersCluster", "c1", A)]}, compartments=[A, B])
    out = {r.id: r for r in OkeCollector().collect(ctx)}
    assert out["c1"].details["node_pools"] == 2 and out["c1"].refs["vcn_id"] == "vcn1"
    assert out["p1"].health == "OK" and out["p1"].details["nodes"] == {"ACTIVE": 2}
    assert out["p2"].health == "CRITICAL" and out["p2"].problem == "critical"
    assert out["p2"].refs["cluster_id"] == "c1"


def test_node_pool_health_rules():
    from ocimon.collectors.oke import node_pool_health
    assert node_pool_health([]) == "UNKNOWN"
    assert node_pool_health(["ACTIVE", "ACTIVE"]) == "OK"
    assert node_pool_health(["ACTIVE", "CREATING"]) == "WARNING"
    assert node_pool_health(["ACTIVE", "INACTIVE"]) == "WARNING"
    assert node_pool_health(["ACTIVE", "FAILED"]) == "CRITICAL"


def test_oke_skips_region_without_clusters(make_ctx):
    client = FakeClient()  # any call would fail the test
    ctx, _ = make_ctx(OkeCollector(), {oci.container_engine.ContainerEngineClient: client},
                      search={}, compartments=[A])
    assert OkeCollector().collect(ctx) == []


# --- networking -----------------------------------------------------------------

def test_network_summary(make_ctx):
    vn = FakeClient(
        list_vcns=by_compartment({A: [ns(id="v1", display_name="hub", compartment_id=A, lifecycle_state="AVAILABLE",
                                         cidr_blocks=["10.0.0.0/16"], cidr_block=None, dns_label="hub",
                                         time_created=NOW)]}),
        list_subnets=by_compartment({A: [ns(id="s1", display_name="pub", compartment_id=A, vcn_id="v1",
                                            lifecycle_state="AVAILABLE", cidr_block="10.0.1.0/24",
                                            prohibit_public_ip_on_vnic=False, availability_domain=None,
                                            dns_label="pub", time_created=NOW)]}),
        list_nat_gateways=by_compartment({B: [ns(id="n1", display_name="nat", compartment_id=B, vcn_id="v1",
                                                 lifecycle_state="AVAILABLE", nat_ip="1.1.1.1", block_traffic=False,
                                                 time_created=NOW)]}),
    )
    search = {"Vcn": [search_item("Vcn", "v1", A)], "Subnet": [search_item("Subnet", "s1", A)],
              "NatGateway": [search_item("NatGateway", "n1", B)]}
    ctx, run = make_ctx(NetworkCollector(), {oci.core.VirtualNetworkClient: vn}, search=search, compartments=[A, B])
    out = {r.id: r for r in NetworkCollector().collect(ctx)}
    assert out["v1"].type == "vcn" and out["v1"].details["cidr_blocks"] == ["10.0.0.0/16"]
    assert out["s1"].details["access"] == "Public" and out["s1"].details["scope"] == "Regional"
    assert out["n1"].type == "nat_gateway" and out["n1"].refs["vcn_id"] == "v1"
    # Only list methods for types Search found were called
    assert {c[0] for c in vn.calls} == {"list_vcns", "list_subnets", "list_nat_gateways"}
    assert run.errors() == []


# --- object storage -------------------------------------------------------------

def test_buckets_public_flag_and_denied_get(make_ctx):
    summaries = [ns(name="public-assets", compartment_id=A, time_created=NOW),
                 ns(name="private", compartment_id=A, time_created=NOW),
                 ns(name="secret", compartment_id=A, time_created=NOW)]
    full = {
        "public-assets": ns(id="ocid1.bucket.oc1.x.pub", public_access_type="ObjectRead", storage_tier="Standard",
                            versioning="Disabled", approximate_count=10, approximate_size=1024),
        "private": ns(id="ocid1.bucket.oc1.x.priv", public_access_type="NoPublicAccess", storage_tier="Archive",
                      versioning="Enabled", approximate_count=0, approximate_size=0),
    }

    def get_bucket(ns_, name, fields=None):
        assert ns_ == "ns1" and "approximateCount" in fields
        return full[name] if name in full else service_error(404, "BucketNotFound")

    client = FakeClient(list_buckets=lambda ns_, comp: summaries if comp == A else [], get_bucket=get_bucket)
    search = {"Bucket": [search_item("Bucket", "ocid1.bucket.oc1.x.secret", A, name="secret")]}
    ctx, run = make_ctx(ObjectStorageCollector(), {oci.object_storage.ObjectStorageClient: client},
                        search=search, compartments=[A])
    out = {r.name: r for r in ObjectStorageCollector().collect(ctx)}
    assert out["public-assets"].details["public"] is True
    assert out["public-assets"].details["visibility"] == "Public (read)"
    assert out["private"].details["public"] is False and out["private"].details["objects"] == 0
    # GetBucket denied: OCID recovered from Search, visibility unknown
    assert out["secret"].id == "ocid1.bucket.oc1.x.secret" and out["secret"].details["public"] is None
    assert out["public-assets"].refs == {"namespace": "ns1", "bucket": "public-assets"}
    assert run.errors()[0].operation == "GetBucket"


# --- serverless -----------------------------------------------------------------

def test_functions_and_api_gateway(make_ctx):
    fn = FakeClient(
        list_applications=by_compartment({A: [ns(id="app1", display_name="handlers", compartment_id=A,
                                                 lifecycle_state="ACTIVE", time_created=NOW, shape="GENERIC_X86",
                                                 subnet_ids=["s1"])]}),
        list_functions=lambda app_id: [ns(), ns(), ns()],
    )
    gw = FakeClient(list_gateways=by_compartment({A: Resp(ns(items=[ns(
        id="g1", display_name="api", compartment_id=A, lifecycle_state="ACTIVE", time_created=NOW,
        endpoint_type="PUBLIC", hostname="x.apigateway.oci.com", lifecycle_details="", subnet_id="s1")]))}))
    search = {"FunctionsApplication": [search_item("FunctionsApplication", "app1", A)],
              "ApiGateway": [search_item("ApiGateway", "g1", A)]}
    clients = {oci.functions.FunctionsManagementClient: fn, oci.apigateway.GatewayClient: gw}

    ctx, _ = make_ctx(FunctionsCollector(), clients, search=search, compartments=[A])
    (app,) = FunctionsCollector().collect(ctx)
    assert app.details["functions"] == 3

    ctx, _ = make_ctx(ApiGatewayCollector(), clients, search=search, compartments=[A])
    (g,) = ApiGatewayCollector().collect(ctx)
    assert g.details["endpoint_type"] == "PUBLIC" and g.details["lifecycle_details"] is None


def test_pagination_follows_next_page(make_ctx):
    pages = {None: Resp([instance("i1", A)], next_page="p2"), "p2": Resp([instance("i2", A)])}
    compute = FakeClient(list_instances=lambda compartment_id, page=None: pages[page], list_vnic_attachments=[])
    ctx, _ = make_ctx(ComputeCollector(), {oci.core.ComputeClient: compute, oci.core.VirtualNetworkClient: FakeClient()},
                      search={"Instance": [search_item("Instance", "i1", A)]}, compartments=[A])
    assert sorted(r.id for r in ComputeCollector().collect(ctx)) == ["i1", "i2"]
