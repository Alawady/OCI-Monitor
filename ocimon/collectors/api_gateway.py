"""API Gateways."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register


@register
class ApiGatewayCollector(Collector):
    key = "api_gateway"
    label = "API Gateway"
    types = (
        ResourceType("api_gateway", "API Gateways", "serverless", "ApiGateway", "api-gateway/gateways/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.apigateway.GatewayClient)  # noqa: E731
        listed = ctx.per_compartment("ListGateways", ctx.compartments_with("ApiGateway"),
                                     lambda c: list_all(client().list_gateways, compartment_id=c))
        return [
            Resource(
                id=g.id, name=g.display_name, type="api_gateway", region=ctx.region,
                compartment_id=g.compartment_id, state=g.lifecycle_state, time_created=g.time_created,
                details={"endpoint_type": g.endpoint_type, "hostname": g.hostname,
                         "lifecycle_details": g.lifecycle_details or None},
                refs={"subnet_id": g.subnet_id},
            )
            for items in listed.values() for g in items
        ]
