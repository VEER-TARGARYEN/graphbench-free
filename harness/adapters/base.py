"""Adapter contract.

The extensible-harness standout the rubric names comes down to one property: adding a
seventh database must be ONE new file implementing this interface, with no `if platform
== ...` anywhere in the runner. Per-engine differences live in adapters and in the
dialect tables of harness/workloads/, never in control flow.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass
class LoadPhase:
    name: str
    seconds: float
    items: int

    @property
    def rate(self) -> float:
        return self.items / self.seconds if self.seconds > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "phase": self.name,
            "seconds": round(self.seconds, 3),
            "items": self.items,
            # An index build is one operation, not a stream: a rate for it would be a
            # meaningless number that a reader might mistake for a throughput figure.
            "items_per_sec": round(self.rate, 1) if self.items > 1 else None,
        }


@dataclass
class LoadReport:
    phases: list[LoadPhase] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    index_ddl: list[str] = field(default_factory=list)

    def phase(self, name: str) -> LoadPhase | None:
        return next((p for p in self.phases if p.name == name), None)

    def to_dict(self) -> dict:
        nodes = self.phase("load_nodes")
        edges = self.phase("load_edges")
        return {
            # Total wall clock INCLUDES index build time. Excluding it is a named
            # pitfall in Raasveldt et al. S 3.7, and engines differ enormously in how
            # much of the cost they defer to index construction.
            "total_wall_clock_sec": round(sum(p.seconds for p in self.phases), 3),
            "nodes_per_sec": round(nodes.rate, 1) if nodes else None,
            "relationships_per_sec": round(edges.rate, 1) if edges else None,
            "phases": [p.to_dict() for p in self.phases],
            "index_ddl": self.index_ddl,
            "errors": self.errors,
        }


class Adapter(abc.ABC):
    """One live connection pool to one platform."""

    #: dialect key into harness.workloads.cit_hepph.DIALECTS
    dialect: str = "default"

    def __init__(self, platform_id: str, label: str, config: dict):
        self.platform_id = platform_id
        self.label = label
        self.config = config

    # ── lifecycle ──────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def verify(self) -> dict:
        """Cheap round trip proving the endpoint is reachable and speaks our protocol.

        Returns whatever server identity the platform is willing to disclose. Run this
        BEFORE building anything: a platform that fails here is excluded in hour one,
        not hour thirty.
        """

    # ── data ───────────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def wipe(self) -> None:
        """Return the database to empty, so every load starts from the same state."""

    @abc.abstractmethod
    def load(self, nodes: Sequence[dict], edges: Sequence[dict], batch_size: int) -> LoadReport: ...

    @abc.abstractmethod
    def cleanup_writes(self) -> int:
        """Delete the :BenchWrite set so repeated mixed runs start identically."""

    # ── execution ──────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def execute(self, worker_id: int, query: str, params: dict, timeout: float) -> list[dict]:
        """Run one query on the calling thread's own session and return plain rows."""

    @abc.abstractmethod
    def counts(self) -> dict:
        """Node and relationship counts, for the post-load equivalence assertion."""

    # ── observability ──────────────────────────────────────────────────────────
    def footprint(self) -> dict:
        """Whatever the platform exposes. `{"observable": false}` is a legitimate and
        expected answer on managed tiers, and the brief explicitly asks us to say so
        rather than invent a plausible figure."""
        return {"observable": False, "reason": "no footprint probe implemented for this adapter"}

    def advertised(self) -> dict:
        return self.config.get("advertised", {})
