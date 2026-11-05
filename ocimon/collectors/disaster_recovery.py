"""Full Stack Disaster Recovery: protection groups and DR plans, with their last run."""

from __future__ import annotations

from typing import Any

import oci

from ..models import Resource, ResourceType
from .base import Collector, RegionContext, list_all, register

# A DR plan whose last run ended like this needs a look before it is relied on.
_BAD_RUNS = {"FAILED", "CANCELED", "PAUSED"}


def _last_run(executions: list[Any]) -> dict[str, Any] | None:
    if not executions:
        return None
    e = max(executions, key=lambda x: getattr(x, "time_created", None) or 0)
    when = getattr(e, "time_ended", None) or getattr(e, "time_started", None) or getattr(e, "time_created", None)
    return {"type": e.plan_execution_type, "state": e.lifecycle_state, "at": when.isoformat() if when else None}


def _run_text(run: dict[str, Any] | None) -> str:
    if not run:
        return "Never run"
    kind = (run["type"] or "").replace("_", " ").lower()
    return f"{kind} {run['state'].lower()} ({(run['at'] or '')[:10]})".strip()


@register
class DisasterRecoveryCollector(Collector):
    key = "disaster_recovery"
    label = "Full Stack Disaster Recovery"
    types = (
        ResourceType("dr_protection_group", "DR protection groups", "dr", "DrProtectionGroup",
                     "disaster-recovery/dr-protection-groups/{id}"),
        ResourceType("dr_plan", "DR plans", "dr", "DrPlan",
                     "disaster-recovery/dr-protection-groups/{dr_protection_group_id}/dr-plans/{id}"),
    )

    def collect(self, ctx: RegionContext) -> list[Resource]:
        client = lambda: ctx.client(oci.disaster_recovery.DisasterRecoveryClient)  # noqa: E731
        listed = ctx.per_compartment(
            "ListDrProtectionGroups", ctx.compartments_with("DrProtectionGroup"),
            lambda c: list_all(client().list_dr_protection_groups, compartment_id=c))
        groups = [g for items in listed.values() for g in items]
        if not groups:
            return []
        live = [g for g in groups if g.lifecycle_state != "DELETED"]
        key = lambda g: g.id  # noqa: E731
        comp = lambda g: g.compartment_id  # noqa: E731
        # Members aren't in the list summary; one GetDrProtectionGroup each (few groups per tenancy).
        full = ctx.map("GetDrProtectionGroup", live, lambda g: client().get_dr_protection_group(g.id).data,
                       key=key, compartment=comp)
        plans = ctx.map("ListDrPlans", live, lambda g: list_all(client().list_dr_plans, g.id), key=key, compartment=comp)
        runs = ctx.map("ListDrPlanExecutions", live, lambda g: list_all(client().list_dr_plan_executions, g.id),
                       key=key, compartment=comp)

        out = []
        for g in groups:
            group_runs = runs.get(g.id) or []
            last = _last_run(group_runs)
            members = getattr(full.get(g.id), "members", None)
            out.append(Resource(
                id=g.id, name=g.display_name, type="dr_protection_group", region=ctx.region,
                compartment_id=g.compartment_id, state=g.lifecycle_state, time_created=g.time_created,
                health="WARNING" if last and last["state"] in _BAD_RUNS else None,
                details={
                    "role": (g.role or "").capitalize() or None,
                    "peer_region": g.peer_region,
                    "members": len(members) if members is not None else None,
                    "plans": len(plans.get(g.id) or []),
                    "last_run": _run_text(last),
                    "sub_state": g.lifecycle_sub_state,
                    "lifecycle_details": g.life_cycle_details or None,
                },
                refs={"peer_id": g.peer_id},
            ))
            for p in plans.get(g.id) or []:
                plan_last = _last_run([e for e in group_runs if e.plan_id == p.id])
                # NEEDS_REFRESH / NEEDS_VERIFICATION live in the sub-state; surface them as the state.
                state = p.lifecycle_sub_state if p.lifecycle_sub_state in ("NEEDS_REFRESH", "NEEDS_VERIFICATION") \
                    else p.lifecycle_state
                out.append(Resource(
                    id=p.id, name=p.display_name, type="dr_plan", region=ctx.region,
                    compartment_id=p.compartment_id, state=state, time_created=p.time_created,
                    health="WARNING" if plan_last and plan_last["state"] in _BAD_RUNS else None,
                    details={
                        "plan_type": (p.type or "").replace("_", " ").capitalize() or None,
                        "peer_region": p.peer_region,
                        "last_run": _run_text(plan_last),
                        "lifecycle_details": p.life_cycle_details or None,
                    },
                    refs={"dr_protection_group_id": p.dr_protection_group_id},
                ))
        return out
