"""Realistic fake tenancy for `--demo`: several compartments and regions, with
a mix of healthy, stopped, transitional and failing resources plus a few
permission errors, so every part of the UI has something to show."""

from __future__ import annotations

import random
import string
import time
from datetime import datetime, timedelta, timezone

from . import metrics as metric_signals
from .models import Compartment, ErrorRecord, Resource
from .snapshot import Snapshot

JED, RUH, PHX = "me-jeddah-1", "me-riyadh-1", "us-phoenix-1"
REGIONS = [JED, RUH, PHX]


class _Builder:
    def __init__(self, tick: int) -> None:
        self.rng = random.Random(42)  # stable OCIDs across refreshes
        self.tick = tick
        self.now = datetime.now(timezone.utc)
        self.resources: list[Resource] = []

    def ocid(self, kind: str, region: str | None = None) -> str:
        tail = "".join(self.rng.choices(string.ascii_lowercase + string.digits, k=52))
        return f"ocid1.{kind}.oc1.{region or ''}.aaaaaaaa{tail}"

    def created(self) -> datetime:
        return self.now - timedelta(days=self.rng.randint(3, 700), hours=self.rng.randint(0, 23))

    def add(self, kind: str, rtype: str, name: str, region: str, comp: str, state: str, **kw) -> Resource:
        r = Resource(id=self.ocid(kind, region), name=name, type=rtype, region=region, compartment_id=comp,
                     state=state, time_created=self.created(), **kw)
        self.resources.append(r)
        return r

    def ip(self, prefix: str) -> str:
        return f"{prefix}.{self.rng.randint(2, 250)}"

    def public_ip(self) -> str:
        return f"{self.rng.choice([129, 130, 150, 152, 158])}.{self.rng.randint(1, 254)}." \
               f"{self.rng.randint(1, 254)}.{self.rng.randint(1, 254)}"

    def instance(self, name: str, region: str, comp: str, state: str, shape: str = "VM.Standard.E5.Flex",
                 ocpus: float = 2, mem: float = 16, ad: int = 1, net: str = "10.0.1", public: bool = False,
                 volumes: int = 0) -> Resource:
        inst = self.add("instance", "instance", name, region, comp, state, details={
            "shape": shape, "ocpus": ocpus, "memory_gb": mem, "availability_domain": f"AD-{ad}",
            "fault_domain": f"FAULT-DOMAIN-{self.rng.randint(1, 3)}",
            "private_ips": [self.ip(net)] if state != "TERMINATED" else [],
            "public_ips": [self.public_ip()] if public and state == "RUNNING" else [],
            "hostname": name,
        })
        if state != "TERMINATED":
            self.add("bootvolume", "boot_volume", f"{name} (Boot Volume)", region, comp, "AVAILABLE",
                     details={"size_gb": 50 if "Flex" in shape else 100, "vpus_per_gb": 10,
                              "availability_domain": f"AD-{ad}", "attached": True},
                     refs={"instance_id": inst.id})
        for i in range(volumes):
            self.add("volume", "volume", f"{name}-data-{i + 1}", region, comp, "AVAILABLE",
                     details={"size_gb": self.rng.choice([256, 512, 1024]), "vpus_per_gb": 20,
                              "availability_domain": f"AD-{ad}", "attached": True},
                     refs={"instance_id": inst.id})
        return inst

    def lb(self, name: str, region: str, comp: str, health: str, sets: dict[str, str], public: bool = True):
        self.add("loadbalancer", "load_balancer", name, region, comp, "ACTIVE", health=health, details={
            "shape": "flexible", "visibility": "Public" if public else "Private",
            "public_ips": [self.public_ip()] if public else [], "private_ips": [] if public else [self.ip("10.0.2")],
            "backend_sets": [{"name": n, "status": s, "backends": self.rng.randint(2, 4)} for n, s in sets.items()],
            "listeners": len(sets),
        })

    def bucket(self, name: str, region: str, comp: str, public: bool = False, objects: int | None = None):
        self.add("bucket", "bucket", name, region, comp, "ACTIVE", details={
            "visibility": "Public (read)" if public else "Private", "public": public,
            "storage_tier": "Standard", "versioning": self.rng.choice(["Enabled", "Disabled"]),
            "objects": objects if objects is not None else self.rng.randint(10, 250_000),
            "size_bytes": self.rng.randint(10**6, 5 * 10**12),
        }, refs={"namespace": "acmecorp", "bucket": name})

    def network(self, region: str, comp: str, vcn_name: str, cidr: str, nat: bool = True, drg: bool = False):
        vcn = self.add("vcn", "vcn", vcn_name, region, comp, "AVAILABLE",
                       details={"cidr_blocks": [cidr], "dns_label": vcn_name.replace("-", "")[:15]})
        base = cidr.rsplit(".", 2)[0]
        for label, public in (("public", True), ("app", False), ("db", False)):
            self.add("subnet", "subnet", f"{vcn_name}-{label}", region, comp, "AVAILABLE",
                     details={"cidr": f"{base}.{self.rng.randint(0, 9)}.0/24",
                              "access": "Public" if public else "Private", "scope": "Regional", "dns_label": label},
                     refs={"vcn_id": vcn.id})
        self.add("internetgateway", "internet_gateway", f"{vcn_name}-igw", region, comp, "AVAILABLE",
                 details={"enabled": True}, refs={"vcn_id": vcn.id})
        if nat:
            self.add("natgateway", "nat_gateway", f"{vcn_name}-nat", region, comp, "AVAILABLE",
                     details={"nat_ip": self.public_ip(), "blocking_traffic": False}, refs={"vcn_id": vcn.id})
        self.add("servicegateway", "service_gateway", f"{vcn_name}-sgw", region, comp, "AVAILABLE",
                 details={"services": [f"All {region.upper()} Services In Oracle Services Network"],
                          "blocking_traffic": False}, refs={"vcn_id": vcn.id})
        if drg:
            self.add("drg", "drg", f"{vcn_name}-drg", region, comp, "AVAILABLE", details={})
        return vcn


def build_demo(tick: int = 0) -> Snapshot:
    started = time.time()
    b = _Builder(tick)
    tenancy = b.ocid("tenancy")
    comps: dict[str, Compartment] = {tenancy: Compartment(tenancy, "acme-corp", None)}

    def comp(name: str, parent: str, desc: str = "") -> str:
        cid = b.ocid("compartment")
        comps[cid] = Compartment(cid, name, parent, description=desc)
        return cid

    prod = comp("Production", tenancy, "Customer-facing workloads")
    prod_app = comp("prod-app", prod, "Web and application tier")
    prod_data = comp("prod-data", prod, "Databases and storage")
    staging = comp("Staging", tenancy, "Pre-production")
    dev = comp("Development", tenancy, "Engineering workloads")
    sandbox = comp("dev-sandbox", dev, "Personal experiments")
    shared = comp("SharedServices", tenancy, "Hub networking")
    security = comp("Security", tenancy, "Restricted: audit and Cloud Guard")

    # --- networking -------------------------------------------------------
    hub = b.network(JED, shared, "hub-vcn", "10.0.0.0/16", drg=True)
    b.network(JED, prod, "prod-vcn", "10.10.0.0/16")
    b.network(RUH, shared, "dr-vcn", "10.20.0.0/16", drg=True)
    b.network(PHX, dev, "dev-vcn", "10.30.0.0/16", nat=False)
    b.add("localpeeringgateway", "local_peering_gateway", "hub-to-prod-lpg", JED, shared, "AVAILABLE",
          details={"peering_status": "PEERED", "peer_cidr": "10.10.0.0/16"}, refs={"vcn_id": hub.id})

    # --- production (Jeddah) ----------------------------------------------
    for i in range(1, 4):
        b.instance(f"prod-web-0{i}", JED, prod_app, "RUNNING", ocpus=2, mem=32, ad=1, net="10.10.1")
    for i in range(1, 3):
        b.instance(f"prod-app-0{i}", JED, prod_app, "RUNNING", ocpus=4, mem=64, ad=1, net="10.10.2", volumes=1)
    b.instance("prod-batch-01", JED, prod_app, "STOPPED", ocpus=8, mem=128, net="10.10.2")
    b.instance("bastion-01", JED, shared, "RUNNING", shape="VM.Standard.A1.Flex", ocpus=1, mem=6,
               net="10.0.0", public=True)
    b.lb("prod-public-lb", JED, prod_app, "WARNING", {"web-bs": "WARNING", "api-bs": "OK"})
    b.add("networkloadbalancer", "network_load_balancer", "prod-internal-nlb", JED, prod_app, "ACTIVE",
          health="OK", details={"visibility": "Private", "public_ips": [], "private_ips": ["10.10.2.10"],
                                "backend_sets": [{"name": "grpc-bs", "status": "OK"}], "listeners": 1})

    cluster = b.add("cluster", "oke_cluster", "prod-oke", JED, prod_app, "ACTIVE", details={
        "kubernetes_version": "v1.31.1", "cluster_type": "ENHANCED_CLUSTER", "node_pools": 2,
        "public_endpoint": None, "private_endpoint": "10.10.3.4:6443"})
    b.add("nodepool", "oke_node_pool", "pool-general", JED, prod_app, "ACTIVE", health="OK", details={
        "kubernetes_version": "v1.31.1", "node_shape": "VM.Standard.E5.Flex", "ocpus": 4, "memory_gb": 64,
        "size": 3, "nodes": {"ACTIVE": 3}}, refs={"cluster_id": cluster.id})
    # Alternates between a rolling update and settled, to show refreshes changing state.
    rolling = tick % 2 == 0
    b.add("nodepool", "oke_node_pool", "pool-batch", JED, prod_app, "UPDATING" if rolling else "ACTIVE",
          health=None if rolling else "OK", details={
              "kubernetes_version": "v1.31.1", "node_shape": "VM.Standard.E5.Flex", "ocpus": 8, "memory_gb": 128,
              "size": 2, "nodes": {"ACTIVE": 1, "UPDATING": 1} if rolling else {"ACTIVE": 2}},
          refs={"cluster_id": cluster.id})

    b.add("autonomousdatabase", "autonomous_database", "prodadw", JED, prod_data, "AVAILABLE", details={
        "workload": "Data Warehouse", "version": "23ai", "compute": "8 ECPU", "storage_tb": 2,
        "free_tier": False, "dedicated": False, "db_name": "PRODADW"})
    b.add("autonomousdatabase", "autonomous_database", "prodatp", JED, prod_data, "AVAILABLE_NEEDS_ATTENTION",
          details={"workload": "Transaction Processing", "version": "19c", "compute": "4 ECPU", "storage_tb": 1,
                   "free_tier": False, "dedicated": False, "db_name": "PRODATP"})
    b.add("dbsystem", "db_system", "erp-db", JED, prod_data, "AVAILABLE", details={
        "shape": "VM.Standard.E5.Flex", "version": "19.24.0.0", "edition": "ENTERPRISE_EDITION_HIGH_PERFORMANCE",
        "cpu_cores": 4, "nodes": 2, "storage_gb": 1024, "hostname": "erpdb", "availability_domain": "AD-1"})
    b.add("mysqldbsystem", "mysql_db_system", "orders-mysql", JED, prod_data, "ACTIVE", details={
        "version": "8.4.3", "shape": "MySQL.8", "highly_available": True,
        "heatwave": "2 x HeatWave.512GB (ACTIVE)", "endpoint": "10.10.4.12:3306", "availability_domain": "AD-1"})
    b.bucket("prod-backups", JED, prod_data)
    b.bucket("prod-public-assets", JED, prod_data, public=True)
    b.bucket("prod-logs-archive", JED, prod_data)
    b.add("volume", "volume", "erp-archive-vol", JED, prod_data, "AVAILABLE", details={
        "size_gb": 2048, "vpus_per_gb": 0, "availability_domain": "AD-1", "attached": False})
    b.add("fnapp", "functions_application", "prod-event-handlers", JED, prod_app, "ACTIVE",
          details={"functions": 6, "shape": "GENERIC_X86", "subnets": 1})
    b.add("apigateway", "api_gateway", "prod-public-api", JED, prod_app, "ACTIVE", details={
        "endpoint_type": "PUBLIC", "hostname": "abc123xyz.apigateway.me-jeddah-1.oci.customer-oci.com",
        "lifecycle_details": None})

    # --- disaster recovery (Riyadh) ---------------------------------------
    b.instance("dr-app-01", RUH, prod_app, "STOPPED", ocpus=4, mem=64, net="10.20.2", volumes=1)
    b.instance("dr-app-02", RUH, prod_app, "STOPPED", ocpus=4, mem=64, net="10.20.2")
    b.add("mysqldbsystem", "mysql_db_system", "orders-mysql-replica", RUH, prod_data, "FAILED", details={
        "version": "8.4.3", "shape": "MySQL.4", "highly_available": False, "heatwave": "Not attached",
        "endpoint": "10.20.4.12:3306", "availability_domain": "AD-1"})
    b.add("autonomousdatabase", "autonomous_database", "prodatp_dr", RUH, prod_data, "STANDBY", details={
        "workload": "Transaction Processing", "version": "19c", "compute": "4 ECPU", "storage_tb": 1,
        "free_tier": False, "dedicated": False, "db_name": "PRODATP"})
    b.bucket("prod-backups-replica", RUH, prod_data)

    # --- staging (Jeddah) -------------------------------------------------
    b.instance("stg-web-01", JED, staging, "RUNNING", net="10.10.11")
    b.instance("stg-app-01", JED, staging, "PROVISIONING" if tick % 3 == 0 else "RUNNING", net="10.10.12")
    b.instance("stg-worker-01", JED, staging, "STARTING", net="10.10.12")
    b.lb("stg-lb", JED, staging, "CRITICAL", {"web-bs": "CRITICAL"})
    b.add("mysqldbsystem", "mysql_db_system", "stg-mysql", JED, staging, "INACTIVE", details={
        "version": "8.4.3", "shape": "MySQL.2", "highly_available": False, "heatwave": "Not attached",
        "endpoint": "10.10.14.5:3306", "availability_domain": "AD-1"})

    # --- development (Phoenix) --------------------------------------------
    b.instance("dev-box-alice", PHX, dev, "RUNNING", shape="VM.Standard.A1.Flex", ocpus=2, mem=12, net="10.30.1",
               public=True)
    b.instance("dev-box-bob", PHX, dev, "STOPPED", shape="VM.Standard.A1.Flex", ocpus=2, mem=12, net="10.30.1")
    b.instance("old-load-test", PHX, dev, "TERMINATED", net="10.30.1")
    b.instance("ci-runner-01", PHX, dev, "RUNNING", ocpus=4, mem=16, net="10.30.2", volumes=1)
    for n in ("scratch-1", "scratch-2"):
        b.add("volume", "volume", n, PHX, dev, "AVAILABLE", details={
            "size_gb": 100, "vpus_per_gb": 10, "availability_domain": "AD-1", "attached": False})
    b.add("autonomousdatabase", "autonomous_database", "devjson", PHX, dev, "STOPPED", details={
        "workload": "JSON", "version": "23ai", "compute": "2 ECPU", "storage_tb": 0.02, "free_tier": True,
        "dedicated": False, "db_name": "DEVJSON"})
    b.add("fnapp", "functions_application", "dev-experiments", PHX, dev, "ACTIVE",
          details={"functions": 3, "shape": "GENERIC_ARM", "subnets": 1})
    b.add("cluster", "oke_cluster", "dev-oke", PHX, dev, "FAILED", details={
        "kubernetes_version": "v1.30.1", "cluster_type": "BASIC_CLUSTER", "node_pools": 0,
        "public_endpoint": None, "private_endpoint": None,
        "lifecycle_details": "Cluster creation failed: subnet has no route to the internet"})
    b.instance("sandbox-01", PHX, sandbox, "RUNNING", shape="VM.Standard.E2.1.Micro", ocpus=1, mem=1,
               net="10.30.5", public=True)
    b.bucket("sandbox-tmp", PHX, sandbox, objects=12)

    # A bucket we could see in Search but not read in detail.
    b.add("bucket", "bucket", "security-audit-logs", JED, security, "ACTIVE", source="search",
          details={"note": "Details unavailable; listed from Resource Search"})

    # Monitoring signals, as the metrics phase would attach them.
    hot = {"prod-app-02": {"memory_pct": 94.2}, "orders-mysql": {"storage_pct": 88.4}}
    for r in b.resources:
        if r.type == "instance" and r.state == "RUNNING":
            metric_signals.apply(r, hot.get(r.name, {
                "cpu_pct": round(b.rng.uniform(3, 65), 1), "memory_pct": round(b.rng.uniform(20, 80), 1), "fs_status": 0}))
        elif r.type == "mysql_db_system" and r.state == "ACTIVE":
            metric_signals.apply(r, {"cpu_pct": round(b.rng.uniform(5, 40), 1), "memory_pct": round(b.rng.uniform(40, 75), 1),
                                     "storage_pct": round(b.rng.uniform(10, 60), 1), **hot.get(r.name, {})})

    # Alarms (shown on the Alerts tab).
    def alarm(name: str, region: str, comp: str, state: str, severity: str, namespace: str, query: str,
              hours_ago: float | None, summary: str = "") -> None:
        health = {"CRITICAL": "CRITICAL", "ERROR": "CRITICAL", "WARNING": "WARNING"}.get(severity) if state == "FIRING" else None
        b.add("alarm", "alarm", name, region, comp, state, health=health, details={
            "severity": severity,
            "triggered_at": (b.now - timedelta(hours=hours_ago)).isoformat() if hours_ago is not None else None,
            "namespace": namespace, "query": query,
            "summary": summary or f'Alarm "{name}" is in a "{state}" state.',
            "notifications": 1, "enabled": state != "DISABLED"})

    alarm("prod-app-02 memory > 90%", JED, prod_app, "FIRING", "WARNING", "oci_computeagent",
          "MemoryUtilization[5m]{resourceDisplayName = \"prod-app-02\"}.mean() > 90", 3.5,
          'Alarm "prod-app-02 memory > 90%" is in a "FIRING" state; because 1 metrics meet the trigger rule.')
    alarm("stg-lb unhealthy backends", JED, staging, "FIRING", "CRITICAL", "oci_lbaas",
          "UnHealthyBackendServers[1m].max() > 0", 26)
    alarm("orders-mysql-replica down", RUH, prod_data, "FIRING", "CRITICAL", "oci_mysql_database",
          "CPUUtilization[1m].absent()", 50)
    alarm("prod-web 5xx rate", JED, prod_app, "OK", "WARNING", "oci_lbaas", "HttpResponses5xx[1m].sum() > 20", 5)
    alarm("orders-mysql storage > 85%", JED, prod_data, "OK", "WARNING", "oci_mysql_database",
          "DbVolumeUtilization[5m].max() > 85", 20)
    alarm("bastion CPU > 90%", JED, shared, "OK", "WARNING", "oci_computeagent", "CpuUtilization[5m].mean() > 90", 240)
    alarm("ci-runner disk IO", PHX, dev, "SUPPRESSED", "INFO", "oci_computeagent", "DiskIopsWritten[5m].mean() > 5000", 72)
    alarm("legacy batch check", JED, prod_app, "DISABLED", "WARNING", "oci_computeagent",
          "CpuUtilization[5m].mean() > 95", None)
    for name in ("prod-web-01", "prod-web-02", "prod-web-03"):
        alarm(f"{name} memory > 90%", JED, prod_app, "OK", "WARNING", "oci_computeagent",
              f"MemoryUtilization[5m]{{resourceDisplayName = \"{name}\"}}.mean() > 90", None)

    errors = [
        ErrorRecord(JED, "Compute", "ListInstances", "Authorization failed or requested resource not found "
                    "(not found or not authorized)", security, 404, "NotAuthorizedOrNotFound", "permission"),
        ErrorRecord(JED, "Block Storage", "ListVolumes", "Authorization failed or requested resource not "
                    "found (not found or not authorized)", security, 404, "NotAuthorizedOrNotFound",
                    "permission"),
        ErrorRecord(JED, "Object Storage", "GetBucket", "Either the bucket named 'security-audit-logs' does "
                    "not exist in the namespace 'acmecorp' or you are not authorized to access it",
                    security, 404, "BucketNotFound", "permission"),
        ErrorRecord(RUH, "Functions", "ListApplications", "Too many requests for the tenancy (retries "
                    "exhausted)", None, 429, "TooManyRequests", "error"),
    ]
    return Snapshot(resources=b.resources, compartments=comps, root_id=tenancy, regions=REGIONS, errors=errors,
                    started_at=started, finished_at=time.time(), tenancy_name="acme-corp", mode="demo",
                    stats={"search_items": len(b.resources), "search_fallbacks": 1})


class DemoSource:
    def __init__(self, delay: float = 0.8) -> None:
        self.delay = delay
        self.tick = 0

    def __call__(self) -> Snapshot:
        started = time.time()
        time.sleep(self.delay)  # feel like a real refresh in the UI
        snap = build_demo(self.tick)
        snap.started_at = started
        self.tick += 1
        return snap
