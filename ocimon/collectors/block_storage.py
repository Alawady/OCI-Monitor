"""Block volumes and boot volumes, with attachment status."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, Task, list_all, register, short_ad

_ATTACHED = ("ATTACHED", "ATTACHING")


@register
class BlockStorageCollector(Collector):
    key = "block_storage"
    label = "Block Storage"
    types = (
        ResourceType("volume", "Block Volumes", "storage", "Volume", "block-storage/volumes/{id}"),
        ResourceType("boot_volume", "Boot Volumes", "storage", "BootVolume", "block-storage/boot-volumes/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        bs = lambda: ctx.client(oci.core.BlockstorageClient)  # noqa: E731
        compute = lambda: ctx.client(oci.core.ComputeClient)  # noqa: E731
        vol_comps = ctx.compartments_with("Volume")
        boot_comps = ctx.compartments_with("BootVolume")
        # Attachments live in the instance's compartment, which may differ from the volume's.
        att_comps = sorted(set(ctx.compartments_with("Instance")) | set(vol_comps) | set(boot_comps))
        ads = ctx.availability_domains() if (boot_comps or att_comps) else []

        tasks = []
        for c in vol_comps:
            tasks.append(Task(("vol", c), "ListVolumes",
                              lambda c=c: list_all(bs().list_volumes, compartment_id=c), c))
        for c in boot_comps:
            tasks.append(Task(("boot", c), "ListBootVolumes",
                              lambda c=c: list_all(bs().list_boot_volumes, compartment_id=c), c))
        for c in att_comps:
            if vol_comps:
                tasks.append(Task(("vatt", c), "ListVolumeAttachments",
                                  lambda c=c: list_all(compute().list_volume_attachments, compartment_id=c), c))
            if boot_comps:
                for ad in ads:
                    tasks.append(Task(("batt", c, ad), "ListBootVolumeAttachments",
                                      lambda c=c, ad=ad: list_all(compute().list_boot_volume_attachments, ad, c), c))
        results = ctx.run(tasks)
        # If an attachment listing was denied, "not attached" would be a guess.
        attachments_known = all(t.key in results for t in tasks if t.key[0] in ("vatt", "batt"))

        def gather(kind: str) -> list:
            return [x for key, items in results.items() if key[0] == kind for x in items]

        attached_to: dict[str, str] = {}
        for att in gather("vatt"):
            if att.lifecycle_state in _ATTACHED:
                attached_to[att.volume_id] = att.instance_id
        for att in gather("batt"):
            if att.lifecycle_state in _ATTACHED:
                attached_to[att.boot_volume_id] = att.instance_id

        out = []
        for kind, rtype in (("vol", "volume"), ("boot", "boot_volume")):
            for v in gather(kind):
                instance_id = attached_to.get(v.id)
                out.append(Resource(
                    id=v.id,
                    name=v.display_name,
                    type=rtype,
                    region=ctx.region,
                    compartment_id=v.compartment_id,
                    state=v.lifecycle_state,
                    time_created=v.time_created,
                    details={
                        "size_gb": v.size_in_gbs,
                        "vpus_per_gb": v.vpus_per_gb,
                        "availability_domain": short_ad(v.availability_domain),
                        "attached": True if instance_id else (False if attachments_known else None),
                    },
                    refs={"instance_id": instance_id},
                ))
        return out
