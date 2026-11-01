"""Compute instances with shape, sizing, placement and IP addresses."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, Task, list_all, register, short_ad


@register
class ComputeCollector(Collector):
    key = "compute"
    label = "Compute"
    types = (
        ResourceType("instance", "Instances", "compute", "Instance", "compute/instances/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        compute = lambda: ctx.client(oci.core.ComputeClient)  # noqa: E731 (thread-local client)
        comps = ctx.compartments_with("Instance")
        tasks = []
        for c in comps:
            tasks.append(Task(("inst", c), "ListInstances",
                              lambda c=c: list_all(compute().list_instances, compartment_id=c), c))
            tasks.append(Task(("vnic", c), "ListVnicAttachments",
                              lambda c=c: list_all(compute().list_vnic_attachments, compartment_id=c), c))
        results = ctx.run(tasks)

        instances = [i for (kind, _), items in results.items() if kind == "inst" for i in items]
        vnics_by_instance: dict[str, list[str]] = {}
        for (kind, _), items in results.items():
            if kind != "vnic":
                continue
            for att in items:
                if att.lifecycle_state == "ATTACHED" and att.vnic_id:
                    vnics_by_instance.setdefault(att.instance_id, []).append(att.vnic_id)

        live = {i.id for i in instances if i.lifecycle_state not in ("TERMINATED", "TERMINATING")}
        vnic_ids = [v for inst, vs in vnics_by_instance.items() if inst in live for v in vs]
        vnics = ctx.map("GetVnic", vnic_ids,
                        lambda v: ctx.client(oci.core.VirtualNetworkClient).get_vnic(v).data)

        out = []
        for inst in instances:
            attached = [vnics[v] for v in vnics_by_instance.get(inst.id, []) if v in vnics]
            attached.sort(key=lambda v: not v.is_primary)
            sc = inst.shape_config
            out.append(Resource(
                id=inst.id,
                name=inst.display_name,
                type="instance",
                region=ctx.region,
                compartment_id=inst.compartment_id,
                state=inst.lifecycle_state,
                time_created=inst.time_created,
                details={
                    "shape": inst.shape,
                    "ocpus": sc.ocpus if sc else None,
                    "memory_gb": sc.memory_in_gbs if sc else None,
                    "availability_domain": short_ad(inst.availability_domain),
                    "fault_domain": inst.fault_domain,
                    "private_ips": [v.private_ip for v in attached if v.private_ip],
                    "public_ips": [v.public_ip for v in attached if v.public_ip],
                    "hostname": next((v.hostname_label for v in attached if v.hostname_label), None),
                },
            ))
        return out
