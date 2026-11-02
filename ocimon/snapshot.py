"""An immutable result of one refresh, plus compartment-tree helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from .models import Compartment, ErrorRecord, Resource


@dataclass
class Snapshot:
    resources: list[Resource]
    compartments: dict[str, Compartment]
    root_id: str
    regions: list[str]
    errors: list[ErrorRecord] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    tenancy_name: str | None = None
    mode: str = "live"
    stats: dict[str, Any] = field(default_factory=dict)
    # Monotonic run time. Wall-clock finished-started also counts time the machine slept mid-refresh.
    elapsed: float | None = None

    @property
    def duration(self) -> float:
        if self.elapsed is not None:
            return self.elapsed
        return max(0.0, self.finished_at - self.started_at)

    def log_summary(self) -> str:
        return f"{len(self.resources)} resources, {len(self.errors)} errors in {self.duration:.1f}s"

    @cached_property
    def by_id(self) -> dict[str, Resource]:
        return {r.id: r for r in self.resources}

    @cached_property
    def children(self) -> dict[str, list[str]]:
        kids: dict[str, list[str]] = {cid: [] for cid in self.compartments}
        for c in self.compartments.values():
            if c.parent_id in kids and c.id != self.root_id:
                kids[c.parent_id].append(c.id)
        for ids in kids.values():
            ids.sort(key=lambda i: self.compartments[i].name.lower())
        return kids

    def descendants(self, compartment_id: str) -> set[str]:
        """The compartment and everything below it."""
        out, stack = set(), [compartment_id]
        while stack:
            cid = stack.pop()
            if cid in out:
                continue
            out.add(cid)
            stack.extend(self.children.get(cid, []))
        return out

    @cached_property
    def paths(self) -> dict[str, str]:
        """'root/prod/app' style paths, excluding the root's own name."""
        paths: dict[str, str] = {}

        def walk(cid: str, prefix: str) -> None:
            name = self.compartments[cid].name
            path = name if cid == self.root_id else (f"{prefix}/{name}" if prefix else name)
            paths[cid] = path
            for child in self.children.get(cid, []):
                walk(child, "" if cid == self.root_id else path)

        if self.root_id in self.compartments:
            walk(self.root_id, "")
        for cid, c in self.compartments.items():
            paths.setdefault(cid, c.name)
        return paths

    def compartment_name(self, compartment_id: str | None) -> str | None:
        c = self.compartments.get(compartment_id or "")
        return c.name if c else None


def empty_snapshot(mode: str = "live") -> Snapshot:
    return Snapshot(resources=[], compartments={}, root_id="", regions=[], mode=mode)
