"""OCI Monitoring alarms with their current status (firing / OK / suppressed / disabled).

Alarms are shown on the Alerts tab, not among the resources. Two read-only
calls per region: alarm definitions (compartment, query, destinations) and
alarm status (firing state, severity, last trigger time).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, Task, list_all, register

# Alarm severity -> resource health while firing
_SEVERITY_HEALTH = {"CRITICAL": "CRITICAL", "ERROR": "CRITICAL", "WARNING": "WARNING", "INFO": "OK"}


def _suppressed(suppression: Any, now: datetime) -> bool:
    if not suppression:
        return False
    start = getattr(suppression, "time_suppress_from", None)
    until = getattr(suppression, "time_suppress_until", None)
    return (start is None or start <= now) and (until is None or now < until)


def alarm_state(alarm: Any, status: Any | None, now: datetime) -> str:
    if not alarm.is_enabled:
        return "DISABLED"
    if _suppressed(getattr(status, "suppression", None) or alarm.suppression, now):
        return "SUPPRESSED"
    return (status.status if status else None) or "UNKNOWN"


@register
class AlarmCollector(Collector):
    key = "alarms"
    label = "Monitoring alarms"
    types = (
        ResourceType("alarm", "Alarms", "alarms", "Alarm", "monitoring/alarms/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.monitoring.MonitoringClient)  # noqa: E731
        if ctx.root_id == ctx.tenancy_id:
            # One subtree call each from the tenancy covers every compartment.
            comps, subtree = [ctx.root_id], True
        else:
            comps, subtree = ctx.compartments_with("Alarm"), False
        if not comps or not ctx.region_has("Alarm"):
            return []
        tasks = []
        for c in comps:
            tasks.append(Task(("def", c), "ListAlarms", lambda c=c: list_all(
                client().list_alarms, c, compartment_id_in_subtree=subtree, lifecycle_state="ACTIVE"), c))
            tasks.append(Task(("status", c), "ListAlarmsStatus", lambda c=c: list_all(
                client().list_alarms_status, c, compartment_id_in_subtree=subtree), c))
        results = ctx.run(tasks)
        alarms = {a.id: a for key, items in results.items() if key[0] == "def" for a in items}
        statuses = {s.id: s for key, items in results.items() if key[0] == "status" for s in items}

        now = datetime.now(timezone.utc)
        out = []
        for alarm in alarms.values():
            if alarm.compartment_id not in ctx.compartments:
                continue
            status = statuses.get(alarm.id)
            state = alarm_state(alarm, status, now)
            severity = (status.severity if status else None) or alarm.severity
            triggered = status.timestamp_triggered if status else None
            out.append(Resource(
                id=alarm.id,
                name=alarm.display_name,
                type="alarm",
                region=ctx.region,
                compartment_id=alarm.compartment_id,
                state=state,
                health=_SEVERITY_HEALTH.get(severity, "WARNING") if state == "FIRING" else None,
                details={
                    "severity": severity,
                    "triggered_at": triggered.isoformat() if triggered else None,
                    "namespace": alarm.namespace,
                    "query": alarm.query,
                    "summary": (status.alarm_summary if status else None) or alarm.alarm_summary,
                    "notifications": len(alarm.destinations or []),
                    "enabled": bool(alarm.is_enabled),
                },
                refs={"metric_compartment_id": alarm.metric_compartment_id},
            ))
        return out
