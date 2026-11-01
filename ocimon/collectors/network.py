"""VCNs, subnets and gateways (summary only)."""

from __future__ import annotations

from typing import Any, Callable

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, Task, list_all, register

_VCN_CHILD = "networking/vcns/{vcn_id}/"


def _vcn(v: Any) -> dict:
    return {"cidr_blocks": v.cidr_blocks or ([v.cidr_block] if v.cidr_block else []), "dns_label": v.dns_label}


def _subnet(s: Any) -> dict:
    return {
        "cidr": s.cidr_block,
        "access": "Private" if s.prohibit_public_ip_on_vnic else "Public",
        "scope": "Regional" if not s.availability_domain else s.availability_domain.rsplit(":", 1)[-1],
        "dns_label": s.dns_label,
    }


def _igw(g: Any) -> dict:
    return {"enabled": bool(g.is_enabled)}


def _nat(g: Any) -> dict:
    return {"nat_ip": g.nat_ip, "blocking_traffic": bool(g.block_traffic)}


def _sgw(g: Any) -> dict:
    return {"services": [s.service_name for s in (g.services or [])], "blocking_traffic": bool(g.block_traffic)}


def _drg(g: Any) -> dict:
    return {}


def _lpg(g: Any) -> dict:
    return {"peering_status": g.peering_status, "peer_cidr": g.peer_advertised_cidr}


# (type key, label, search type, list method, console path, details fn)
_KINDS: list[tuple[str, str, str, str, str, Callable[[Any], dict]]] = [
    ("vcn", "VCNs", "Vcn", "list_vcns", "networking/vcns/{id}", _vcn),
    ("subnet", "Subnets", "Subnet", "list_subnets", _VCN_CHILD + "subnets/{id}", _subnet),
    ("internet_gateway", "Internet Gateways", "InternetGateway", "list_internet_gateways",
     _VCN_CHILD + "internet-gateways/{id}", _igw),
    ("nat_gateway", "NAT Gateways", "NatGateway", "list_nat_gateways", _VCN_CHILD + "nat-gateways/{id}", _nat),
    ("service_gateway", "Service Gateways", "ServiceGateway", "list_service_gateways",
     _VCN_CHILD + "service-gateways/{id}", _sgw),
    ("drg", "DRGs", "Drg", "list_drgs", "networking/drgs/{id}", _drg),
    ("local_peering_gateway", "Local Peering Gateways", "LocalPeeringGateway", "list_local_peering_gateways",
     _VCN_CHILD + "local-peering-gateways/{id}", _lpg),
]


def _operation(method: str) -> str:
    return "".join(part.capitalize() for part in method.split("_"))


@register
class NetworkCollector(Collector):
    key = "network"
    label = "Networking"
    types = tuple(ResourceType(k, label, "networking", st, console) for k, label, st, _, console, _ in _KINDS)

    def collect(self, ctx: RegionContext) -> list[Resource]:
        vn = lambda: ctx.client(oci.core.VirtualNetworkClient)  # noqa: E731
        tasks = []
        for key, _, search_type, method, _, _ in _KINDS:
            for c in ctx.compartments_with(search_type):
                tasks.append(Task((key, c), _operation(method),
                                  lambda c=c, m=method: list_all(getattr(vn(), m), compartment_id=c), c))
        results = ctx.run(tasks)

        details_fn = {k: fn for k, _, _, _, _, fn in _KINDS}
        out = []
        for (key, _), items in results.items():
            for item in items:
                out.append(Resource(
                    id=item.id, name=item.display_name, type=key, region=ctx.region,
                    compartment_id=item.compartment_id, state=item.lifecycle_state,
                    time_created=getattr(item, "time_created", None),
                    details=details_fn[key](item),
                    refs={"vcn_id": getattr(item, "vcn_id", None)},
                ))
        return out
