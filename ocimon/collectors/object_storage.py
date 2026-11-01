"""Object Storage buckets with public/private visibility."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register

_ACCESS = {
    "NoPublicAccess": "Private",
    "ObjectRead": "Public (read)",
    "ObjectReadWithoutList": "Public (read, no list)",
}


@register
class ObjectStorageCollector(Collector):
    key = "object_storage"
    label = "Object Storage"
    types = (
        ResourceType("bucket", "Buckets", "object_storage", "Bucket",
                     "object-storage/buckets/{namespace}/{bucket}/objects"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        comps = ctx.compartments_with("Bucket")
        if not comps:
            return []
        client = lambda: ctx.client(oci.object_storage.ObjectStorageClient)  # noqa: E731
        ns = ctx.namespace()
        listed = ctx.per_compartment("ListBuckets", comps,
                                     lambda c: list_all(client().list_buckets, ns, c))
        summaries = [b for items in listed.values() for b in items]
        full = ctx.map("GetBucket", summaries,
                       lambda b: client().get_bucket(ns, b.name, fields=["approximateCount", "approximateSize"]).data,
                       key=lambda b: b.name, compartment=lambda b: b.compartment_id)
        # Bucket summaries carry no OCID; Search does, so fall back to it if GetBucket was denied.
        search_ids = {(i.compartment_id, i.display_name): i.identifier for i in ctx.search_items("Bucket")}

        out = []
        for b in summaries:
            f = full.get(b.name)
            access = f.public_access_type if f else None
            out.append(Resource(
                id=(f.id if f and f.id else None) or search_ids.get((b.compartment_id, b.name))
                or f"bucket:{ns}/{b.name}",
                name=b.name, type="bucket", region=ctx.region, compartment_id=b.compartment_id,
                state="ACTIVE", time_created=b.time_created,
                details={
                    "visibility": _ACCESS.get(access, access) if access else None,
                    "public": access not in (None, "NoPublicAccess") if access else None,
                    "storage_tier": f.storage_tier if f else None,
                    "versioning": f.versioning if f else None,
                    "objects": f.approximate_count if f else None,
                    "size_bytes": f.approximate_size if f else None,
                },
                refs={"namespace": ns, "bucket": b.name},
            ))
        return out
