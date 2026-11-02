"""Filtering, summaries and the compartment tree served by the API."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from .collectors import resource_types
from .console import console_url
from .models import CATEGORIES, Resource, isoformat
from .snapshot import Snapshot

if TYPE_CHECKING:
    from .cost import CostReport


def _split(value: str | None) -> set[str]:
    return {v.strip() for v in (value or "").split(",") if v.strip()}


@dataclass
class ResourceQuery:
    compartment: str | None = None
    subtree: bool = True
    types: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    states: set[str] = field(default_factory=set)
    regions: set[str] = field(default_factory=set)
    q: str = ""
    problems_only: bool = False
    include_terminated: bool = False

    @classmethod
    def from_params(cls, *, compartment: str | None = None, subtree: bool = True, type: str | None = None,
                    category: str | None = None, state: str | None = None, region: str | None = None,
                    q: str | None = None, problems: bool = False,
                    include_terminated: bool = False) -> "ResourceQuery":
        return cls(
            compartment=compartment or None,
            subtree=subtree,
            types=_split(type),
            categories=_split(category),
            states={s.upper() for s in _split(state)},
            regions=_split(region),
            q=(q or "").strip().lower(),
            problems_only=problems,
            include_terminated=include_terminated,
        )


def _haystack(snapshot: Snapshot, r: Resource) -> str:
    rtype = resource_types().get(r.type)
    parts: list[Any] = [r.name, r.id, r.type, rtype.label if rtype else "", r.state, r.health or "",
                        r.region, snapshot.paths.get(r.compartment_id, "")]
    for v in r.details.values():
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
        elif isinstance(v, dict):
            parts.extend(str(x) for x in v.values())
        elif v is not None:
            parts.append(v)
    return " ".join(str(p) for p in parts).lower()


def filter_resources(snapshot: Snapshot, query: ResourceQuery) -> list[Resource]:
    types = resource_types()
    allowed_comps = None
    if query.compartment:
        allowed_comps = snapshot.descendants(query.compartment) if query.subtree else {query.compartment}
    tokens = query.q.split()
    # Alarms live on the Alerts tab; they only appear when asked for explicitly.
    want_alarms = "alarm" in query.types or "alarms" in query.categories
    out = []
    for r in snapshot.resources:
        if r.type == "alarm" and not want_alarms:
            continue
        if r.terminated and not query.include_terminated and r.state not in query.states:
            continue
        if allowed_comps is not None and r.compartment_id not in allowed_comps:
            continue
        if query.types and r.type not in query.types:
            continue
        if query.categories and (types[r.type].category if r.type in types else None) not in query.categories:
            continue
        if query.states and r.state not in query.states and (r.health or "") not in query.states:
            continue
        if query.regions and r.region not in query.regions:
            continue
        if query.problems_only and not r.problem:
            continue
        if tokens:
            hay = _haystack(snapshot, r)
            if not all(t in hay for t in tokens):
                continue
        out.append(r)
    return out


def resource_view(snapshot: Snapshot, r: Resource, cost: "CostReport | None" = None) -> dict[str, Any]:
    types = resource_types()
    rtype = types.get(r.type)
    related = []
    for key, value in r.refs.items():
        if not key.endswith("_id") or not value:
            continue
        other = snapshot.by_id.get(value)
        related.append({
            "rel": key[:-3],
            "id": value,
            "name": other.name if other else snapshot.paths.get(value),  # compartments aren't resources
            "type": other.type if other else None,
        })
    return {
        "id": r.id,
        "name": r.name,
        "type": r.type,
        "type_label": rtype.label if rtype else r.type,
        "category": rtype.category if rtype else "other",
        "region": r.region,
        "compartment_id": r.compartment_id,
        "compartment_name": snapshot.compartment_name(r.compartment_id),
        "compartment_path": snapshot.paths.get(r.compartment_id),
        "state": r.state,
        "state_class": r.state_class,
        "health": r.health,
        "health_class": r.health_class,
        "status": r.status,
        "problem": r.problem,
        "time_created": isoformat(r.time_created),
        "details": r.details,
        "related": related,
        "console_url": console_url(r, rtype),
        "source": r.source,
        "cost": cost.for_resource(r) if cost else None,
        "metrics": r.metrics,
        "health_reasons": r.health_reasons,
    }


def _status_counts(resources: Iterable[Resource]) -> dict[str, int]:
    counts = Counter({"ok": 0, "stopped": 0, "warn": 0, "bad": 0, "unknown": 0})
    for r in resources:
        counts[r.status] += 1
    return dict(counts)


def summarize(resources: list[Resource]) -> dict[str, Any]:
    types = resource_types()
    by_type: dict[str, list[Resource]] = {}
    for r in resources:
        by_type.setdefault(r.type, []).append(r)

    type_rows = {}
    for key, items in by_type.items():
        type_rows[key] = {
            "label": types[key].label if key in types else key,
            "category": types[key].category if key in types else "other",
            "total": len(items),
            "by_state": dict(Counter(r.state for r in items)),
            "by_status": _status_counts(items),
            "problems": sum(1 for r in items if r.problem),
        }

    cards = []
    for cat in CATEGORIES:
        items = [r for k, rs in by_type.items() if k in types and types[k].category == cat.key for r in rs]
        cards.append({
            "category": cat.key,
            "label": cat.card,
            "total": len(items),
            "by_status": _status_counts(items),
            "problems": sum(1 for r in items if r.problem),
            "types": sorted({r.type for r in items}),
        })

    problems = [r for r in resources if r.problem]
    return {
        "totals": {
            "resources": len(resources),
            "problems": len(problems),
            "critical": sum(1 for r in problems if r.problem == "critical"),
            "warning": sum(1 for r in problems if r.problem == "warning"),
        },
        "by_state": dict(Counter(r.state for r in resources)),
        "cards": cards,
        "types": type_rows,
    }


def compartment_tree(snapshot: Snapshot, resources: list[Resource]) -> dict[str, Any] | None:
    if snapshot.root_id not in snapshot.compartments:
        return None
    direct = Counter(r.compartment_id for r in resources)
    direct_problems = Counter(r.compartment_id for r in resources if r.problem)

    def build(cid: str) -> dict[str, Any]:
        c = snapshot.compartments[cid]
        children = [build(k) for k in snapshot.children.get(cid, [])]
        return {
            "id": cid,
            "name": c.name,
            "path": snapshot.paths.get(cid),
            "description": c.description,
            "count": direct.get(cid, 0),
            "problems": direct_problems.get(cid, 0),
            "total": direct.get(cid, 0) + sum(ch["total"] for ch in children),
            "total_problems": direct_problems.get(cid, 0) + sum(ch["total_problems"] for ch in children),
            "children": children,
        }

    return build(snapshot.root_id)
