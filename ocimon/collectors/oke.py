"""OKE clusters and node pools (with per-node health)."""

from __future__ import annotations

from collections import Counter

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register

# OKE keeps reporting nodes removed by scale-down or cycling for a while; they aren't part of the pool.
_NODE_GONE = {"DELETED", "DELETING"}


def node_pool_health(node_states: list[str]) -> str:
    if not node_states:
        return "UNKNOWN"
    if "FAILED" in node_states:
        return "CRITICAL"
    if any(s != "ACTIVE" for s in node_states):
        return "WARNING"
    return "OK"


@register
class OkeCollector(Collector):
    key = "oke"
    label = "Kubernetes Engine"
    types = (
        ResourceType("oke_cluster", "Clusters", "containers", "ClustersCluster", "containers/clusters/{id}"),
        # Node pools aren't indexed by Resource Search.
        ResourceType("oke_node_pool", "Node Pools", "containers", None,
                     "containers/clusters/{cluster_id}/node-pools/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.container_engine.ContainerEngineClient)  # noqa: E731
        if not ctx.region_has("ClustersCluster"):
            return []
        listed = ctx.per_compartment("ListClusters", ctx.compartments_with("ClustersCluster"),
                                     lambda c: list_all(client().list_clusters, compartment_id=c))
        clusters = [c for items in listed.values() for c in items]
        if not clusters:
            return []
        # Node pools can live in a different compartment than their cluster.
        pools_listed = ctx.per_compartment("ListNodePools", list(ctx.compartments),
                                           lambda c: list_all(client().list_node_pools, compartment_id=c))
        cluster_ids = {c.id for c in clusters}
        pools = [p for items in pools_listed.values() for p in items if p.cluster_id in cluster_ids]
        detailed = ctx.map("GetNodePool", [p for p in pools if p.lifecycle_state != "DELETED"],
                           lambda p: client().get_node_pool(p.id).data,
                           key=lambda p: p.id, compartment=lambda p: p.compartment_id)

        out = []
        pool_count = Counter(p.cluster_id for p in pools)
        for c in clusters:
            out.append(Resource(
                id=c.id, name=c.name, type="oke_cluster", region=ctx.region,
                compartment_id=c.compartment_id, state=c.lifecycle_state,
                time_created=getattr(c.metadata, "time_created", None) if c.metadata else None,
                details={
                    "kubernetes_version": c.kubernetes_version,
                    "cluster_type": c.type,
                    "node_pools": pool_count.get(c.id, 0),
                    "public_endpoint": getattr(c.endpoints, "public_endpoint", None) if c.endpoints else None,
                    "private_endpoint": getattr(c.endpoints, "private_endpoint", None) if c.endpoints else None,
                    "lifecycle_details": c.lifecycle_details or None,
                },
                refs={"vcn_id": c.vcn_id},
            ))
        for p in pools:
            full = detailed.get(p.id)
            node_states = [n.lifecycle_state for n in (full.nodes or [])
                           if n.lifecycle_state not in _NODE_GONE] if full else []
            size = getattr(p.node_config_details, "size", None) if p.node_config_details else None
            sc = p.node_shape_config
            out.append(Resource(
                id=p.id, name=p.name, type="oke_node_pool", region=ctx.region,
                compartment_id=p.compartment_id, state=p.lifecycle_state,
                health=node_pool_health(node_states) if full and p.lifecycle_state == "ACTIVE" else None,
                details={
                    "kubernetes_version": p.kubernetes_version,
                    "node_shape": p.node_shape,
                    "ocpus": sc.ocpus if sc else None,
                    "memory_gb": sc.memory_in_gbs if sc else None,
                    "size": size,
                    "nodes": dict(Counter(node_states)) if full else None,
                },
                refs={"cluster_id": p.cluster_id},
            ))
        return out
