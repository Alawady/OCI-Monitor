"""Base Database (VM/BM) DB systems, Autonomous Databases and MySQL HeatWave."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register, short_ad


@register
class DbSystemCollector(Collector):
    key = "db_system"
    label = "Database (VM/BM)"
    types = (
        ResourceType("db_system", "DB Systems (VM/BM)", "database", "DbSystem", "dbaas/dbsystems/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.database.DatabaseClient)  # noqa: E731
        listed = ctx.per_compartment("ListDbSystems", ctx.compartments_with("DbSystem"),
                                     lambda c: list_all(client().list_db_systems, compartment_id=c))
        return [
            Resource(
                id=d.id, name=d.display_name, type="db_system", region=ctx.region,
                compartment_id=d.compartment_id, state=d.lifecycle_state, time_created=d.time_created,
                details={
                    "shape": d.shape,
                    "version": d.version,
                    "edition": d.database_edition,
                    "cpu_cores": d.cpu_core_count,
                    "nodes": d.node_count,
                    "storage_gb": d.data_storage_size_in_gbs,
                    "hostname": d.hostname,
                    "availability_domain": short_ad(d.availability_domain),
                },
            )
            for items in listed.values() for d in items
        ]


_WORKLOADS = {"OLTP": "Transaction Processing", "DW": "Data Warehouse", "AJD": "JSON", "APEX": "APEX"}


@register
class AutonomousDatabaseCollector(Collector):
    key = "autonomous_database"
    label = "Autonomous Database"
    types = (
        ResourceType("autonomous_database", "Autonomous Databases", "database", "AutonomousDatabase",
                     "db/adbs/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.database.DatabaseClient)  # noqa: E731
        listed = ctx.per_compartment("ListAutonomousDatabases", ctx.compartments_with("AutonomousDatabase"),
                                     lambda c: list_all(client().list_autonomous_databases, compartment_id=c))
        out = []
        for items in listed.values():
            for a in items:
                compute = a.compute_count if a.compute_count is not None else a.cpu_core_count
                out.append(Resource(
                    id=a.id, name=a.display_name, type="autonomous_database", region=ctx.region,
                    compartment_id=a.compartment_id, state=a.lifecycle_state, time_created=a.time_created,
                    details={
                        "workload": _WORKLOADS.get(a.db_workload, a.db_workload),
                        "version": a.db_version,
                        "compute": f"{compute} {a.compute_model or 'OCPU'}" if compute is not None else None,
                        "storage_tb": a.data_storage_size_in_tbs,
                        "free_tier": bool(a.is_free_tier),
                        "dedicated": bool(a.is_dedicated),
                        "db_name": a.db_name,
                    },
                ))
        return out


@register
class MysqlCollector(Collector):
    key = "mysql"
    label = "MySQL HeatWave"
    types = (
        ResourceType("mysql_db_system", "MySQL HeatWave", "database", "MysqlDbSystem",
                     "mysqlaas/db-systems/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.mysql.DbSystemClient)  # noqa: E731
        listed = ctx.per_compartment("ListMysqlDbSystems", ctx.compartments_with("MysqlDbSystem"),
                                     lambda c: list_all(client().list_db_systems, compartment_id=c))
        out = []
        for items in listed.values():
            for m in items:
                hw = m.heat_wave_cluster
                endpoint = (m.endpoints or [None])[0]
                out.append(Resource(
                    id=m.id, name=m.display_name, type="mysql_db_system", region=ctx.region,
                    compartment_id=m.compartment_id, state=m.lifecycle_state, time_created=m.time_created,
                    details={
                        "version": m.mysql_version,
                        "shape": m.shape_name,
                        "highly_available": bool(m.is_highly_available),
                        "heatwave": (f"{hw.cluster_size} x {hw.shape_name} ({hw.lifecycle_state})"
                                     if hw else "Not attached"),
                        "endpoint": f"{endpoint.ip_address}:{endpoint.port}" if endpoint else None,
                        "availability_domain": short_ad(m.availability_domain),
                    },
                ))
        return out
