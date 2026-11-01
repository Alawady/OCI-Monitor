"""OCI Functions applications."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register


@register
class FunctionsCollector(Collector):
    key = "functions"
    label = "Functions"
    types = (
        ResourceType("functions_application", "Functions Applications", "serverless",
                     "FunctionsApplication", "functions/apps/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.functions.FunctionsManagementClient)  # noqa: E731
        listed = ctx.per_compartment("ListApplications", ctx.compartments_with("FunctionsApplication"),
                                     lambda c: list_all(client().list_applications, compartment_id=c))
        apps = [a for items in listed.values() for a in items]
        counts = ctx.map("ListFunctions", [a for a in apps if a.lifecycle_state == "ACTIVE"],
                         lambda a: len(list_all(client().list_functions, a.id)),
                         key=lambda a: a.id, compartment=lambda a: a.compartment_id)
        return [
            Resource(
                id=a.id, name=a.display_name, type="functions_application", region=ctx.region,
                compartment_id=a.compartment_id, state=a.lifecycle_state, time_created=a.time_created,
                details={"functions": counts.get(a.id), "shape": a.shape, "subnets": len(a.subnet_ids or [])},
            )
            for a in apps
        ]
