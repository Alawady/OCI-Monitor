"""Web Application Firewalls and APM domains."""

from __future__ import annotations

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register


@register
class WafCollector(Collector):
    key = "waf"
    label = "Web Application Firewall"
    types = (
        ResourceType("web_app_firewall", "Web Application Firewalls", "security", "WebAppFirewall",
                     "waf/web-app-firewalls/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.waf.WafClient)  # noqa: E731
        listed = ctx.per_compartment("ListWebAppFirewalls", ctx.compartments_with("WebAppFirewall"),
                                     lambda c: list_all(client().list_web_app_firewalls, compartment_id=c))
        return [
            Resource(
                id=w.id, name=w.display_name, type="web_app_firewall", region=ctx.region,
                compartment_id=w.compartment_id, state=w.lifecycle_state, time_created=w.time_created,
                details={"backend": (w.backend_type or "").replace("_", " ").capitalize() or None,
                         "lifecycle_details": w.lifecycle_details or None},
                # load_balancer_id resolves to the protected LB's name in the UI.
                refs={"load_balancer_id": getattr(w, "load_balancer_id", None),
                      "web_app_firewall_policy_id": w.web_app_firewall_policy_id},
            )
            for items in listed.values() for w in items
        ]


@register
class ApmCollector(Collector):
    key = "apm"
    label = "Application Performance Monitoring"
    types = (
        ResourceType("apm_domain", "APM domains", "security", "ApmDomain", "apm/apm-domains/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.apm_control_plane.ApmDomainClient)  # noqa: E731
        listed = ctx.per_compartment("ListApmDomains", ctx.compartments_with("ApmDomain"),
                                     lambda c: list_all(client().list_apm_domains, compartment_id=c))
        return [
            Resource(
                id=d.id, name=d.display_name, type="apm_domain", region=ctx.region,
                compartment_id=d.compartment_id, state=d.lifecycle_state, time_created=d.time_created,
                details={"free_tier": bool(d.is_free_tier), "description": d.description or None},
            )
            for items in listed.values() for d in items
        ]
