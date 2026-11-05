"""OCI Streaming streams."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register


@register
class StreamingCollector(Collector):
    key = "streaming"
    label = "Streaming"
    types = (
        ResourceType("stream", "Streams", "serverless", "Stream", "storage/streaming/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.streaming.StreamAdminClient)  # noqa: E731
        listed = ctx.per_compartment("ListStreams", ctx.compartments_with("Stream"),
                                     lambda c: list_all(client().list_streams, compartment_id=c))
        return [
            Resource(
                id=s.id, name=s.name, type="stream", region=ctx.region,
                compartment_id=s.compartment_id, state=s.lifecycle_state, time_created=s.time_created,
                details={"partitions": s.partitions, "endpoint": s.messages_endpoint},
                refs={"stream_pool_id": s.stream_pool_id},
            )
            for items in listed.values() for s in items
        ]
