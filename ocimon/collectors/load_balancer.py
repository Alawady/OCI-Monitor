"""Load balancers and network load balancers, with overall and backend-set health."""

from __future__ import annotations

from typing import Any

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register


def backend_set_health(names: list[str], health: Any | None) -> list[dict[str, str]]:
    """Per backend set status from the health summary's name lists."""
    if health is None:
        return [{"name": n, "status": "UNKNOWN"} for n in sorted(names)]
    status = {}
    for attr, value in (("unknown_state_backend_set_names", "UNKNOWN"),
                        ("warning_state_backend_set_names", "WARNING"),
                        ("critical_state_backend_set_names", "CRITICAL")):
        for n in getattr(health, attr, None) or []:
            status[n] = value
    names = sorted(set(names) | set(status))
    return [{"name": n, "status": status.get(n, "OK")} for n in names]


def _ips(addresses: list[Any] | None) -> tuple[list[str], list[str]]:
    public, private = [], []
    for a in addresses or []:
        (public if a.is_public else private).append(a.ip_address)
    return public, private


@register
class LoadBalancerCollector(Collector):
    key = "load_balancer"
    label = "Load Balancer"
    types = (
        ResourceType("load_balancer", "Load Balancers", "load_balancing", "LoadBalancer",
                     "networking/load-balancers/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.load_balancer.LoadBalancerClient)  # noqa: E731
        listed = ctx.per_compartment("ListLoadBalancers", ctx.compartments_with("LoadBalancer"),
                                     lambda c: list_all(client().list_load_balancers, compartment_id=c))
        lbs = [lb for items in listed.values() for lb in items]
        active = [lb for lb in lbs if lb.lifecycle_state == "ACTIVE"]
        health = ctx.map("GetLoadBalancerHealth", active,
                         lambda lb: client().get_load_balancer_health(lb.id).data,
                         key=lambda lb: lb.id, compartment=lambda lb: lb.compartment_id)
        out = []
        for lb in lbs:
            h = health.get(lb.id)
            public, private = _ips(lb.ip_addresses)
            sets = backend_set_health(list((lb.backend_sets or {}).keys()), h)
            for s in sets:
                bset = (lb.backend_sets or {}).get(s["name"])
                s["backends"] = len(bset.backends or []) if bset else None
            out.append(Resource(
                id=lb.id,
                name=lb.display_name,
                type="load_balancer",
                region=ctx.region,
                compartment_id=lb.compartment_id,
                state=lb.lifecycle_state,
                health=(h.status if h else "UNKNOWN") if lb.lifecycle_state == "ACTIVE" else None,
                time_created=lb.time_created,
                details={
                    "shape": lb.shape_name,
                    "visibility": "Private" if lb.is_private else "Public",
                    "public_ips": public,
                    "private_ips": private,
                    "backend_sets": sets,
                    "listeners": len(lb.listeners or {}),
                },
            ))
        return out


@register
class NetworkLoadBalancerCollector(Collector):
    key = "network_load_balancer"
    label = "Network Load Balancer"
    types = (
        ResourceType("network_load_balancer", "Network Load Balancers", "load_balancing",
                     "NetworkLoadBalancer", "networking/load-balancers/network-load-balancer/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.network_load_balancer.NetworkLoadBalancerClient)  # noqa: E731
        listed = ctx.per_compartment("ListNetworkLoadBalancers", ctx.compartments_with("NetworkLoadBalancer"),
                                     lambda c: list_all(client().list_network_load_balancers, compartment_id=c))
        nlbs = [n for items in listed.values() for n in items]
        active = [n for n in nlbs if n.lifecycle_state == "ACTIVE"]
        health = ctx.map("GetNetworkLoadBalancerHealth", active,
                         lambda n: client().get_network_load_balancer_health(n.id).data,
                         key=lambda n: n.id, compartment=lambda n: n.compartment_id)
        out = []
        for n in nlbs:
            h = health.get(n.id)
            public, private = _ips(n.ip_addresses)
            out.append(Resource(
                id=n.id,
                name=n.display_name,
                type="network_load_balancer",
                region=ctx.region,
                compartment_id=n.compartment_id,
                state=n.lifecycle_state,
                health=(h.status if h else "UNKNOWN") if n.lifecycle_state == "ACTIVE" else None,
                time_created=n.time_created,
                details={
                    "visibility": "Private" if n.is_private else "Public",
                    "public_ips": public,
                    "private_ips": private,
                    "backend_sets": backend_set_health(list((n.backend_sets or {}).keys()), h),
                    "listeners": len(n.listeners or {}),
                },
            ))
        return out
