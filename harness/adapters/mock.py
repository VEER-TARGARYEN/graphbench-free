"""In-process mock engine. Never used for published results - only to test the harness.

Two reasons this file exists, both practical:

1. THE HARNESS MUST BE PROVEN BEFORE CLOUD TIME IS SPENT. Free tiers are metered, some
   are deleted after a day idle, and one of them is a 14-day trial. Discovering a
   threading bug in the scheduler on hour 30 against a live endpoint is the worst
   possible time to discover it.

2. CI WITHOUT CREDENTIALS. `scripts/smoke_test.py` exercises the full pipeline - load,
   equivalence, open-loop dispatch, percentile recording, the on-time validity gate -
   with no network and no secrets, so anyone cloning the repo can verify it works.

The simulated latency model includes a THROTTLE KNEE: after a configurable number of
operations the engine slows by a large factor, mimicking a burstable instance draining
its CPU-credit bucket. That is not decoration - it is how we prove the on-time gate
actually fires rather than merely existing in the code.
"""
from __future__ import annotations

import random
import threading
import time
from collections import defaultdict
from typing import Sequence

from ..workloads import cit_hepph as W
from .base import Adapter, LoadPhase, LoadReport


class MockAdapter(Adapter):
    dialect = "default"

    def __init__(self, platform_id: str, label: str, config: dict):
        super().__init__(platform_id, label, config)
        sim = config.get("simulate", {})
        self.base_ms: float = sim.get("base_ms", 3.0)
        self.jitter_ms: float = sim.get("jitter_ms", 1.5)
        self.per_hop_ms: float = sim.get("per_hop_ms", 2.0)
        self.throttle_after: int | None = sim.get("throttle_after")
        self.throttle_factor: float = sim.get("throttle_factor", 12.0)

        self._nodes: dict[int, dict] = {}
        self._out: dict[int, list[int]] = defaultdict(list)
        self._by_date: dict[str, list[int]] = defaultdict(list)
        self._written: set[int] = set()
        self._lock = threading.Lock()
        self._ops = 0
        self._rng = random.Random(1234)

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def verify(self) -> dict:
        return {"reachable": True, "round_trip_ms": self.base_ms, "engine": "mock (not a real database)"}

    # ── simulated cost ─────────────────────────────────────────────────────────
    @staticmethod
    def _precise_sleep(seconds: float) -> None:
        """Sleep accurately enough to simulate single-digit-millisecond latencies.

        A plain time.sleep() is quantised to the OS timer - ~15.6 ms on Windows by
        default - which would make every simulated engine look identical at 16 ms and
        render the whole fixture useless. Sleep the bulk, spin the last 1.5 ms.
        """
        deadline = time.perf_counter() + seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            if remaining > 0.0015:
                time.sleep(remaining - 0.0015)
            else:
                time.sleep(0)

    def _spend(self, hops: int = 0) -> None:
        with self._lock:
            self._ops += 1
            throttled = self.throttle_after is not None and self._ops > self.throttle_after
            jitter = self._rng.random()
        ms = self.base_ms + hops * self.per_hop_ms + jitter * self.jitter_ms
        if throttled:
            ms *= self.throttle_factor
        self._precise_sleep(ms / 1000.0)

    # ── data ───────────────────────────────────────────────────────────────────
    def wipe(self) -> None:
        self._nodes.clear()
        self._out.clear()
        self._by_date.clear()
        self._written.clear()

    def load(self, nodes: Sequence[dict], edges: Sequence[dict], batch_size: int) -> LoadReport:
        report = LoadReport()

        t0 = time.perf_counter()
        for r in nodes:
            self._nodes[r["id"]] = {"id": r["id"], "pub_date": r["d"], "pub_year": r["y"]}
            self._by_date[r["d"]].append(r["id"])
        report.phases.append(LoadPhase("load_nodes", max(1e-6, time.perf_counter() - t0), len(nodes)))

        t0 = time.perf_counter()
        report.index_ddl.append("index_id: (mock) hash index on Paper.id")
        report.phases.append(LoadPhase("index_id", max(1e-6, time.perf_counter() - t0), 1))

        t0 = time.perf_counter()
        for r in edges:
            self._out[r["s"]].append(r["t"])
        report.phases.append(LoadPhase("load_edges", max(1e-6, time.perf_counter() - t0), len(edges)))

        t0 = time.perf_counter()
        report.index_ddl.append("index_secondary: (mock) hash index on Paper.pub_date")
        report.phases.append(LoadPhase("index_secondary", max(1e-6, time.perf_counter() - t0), 1))
        return report

    def cleanup_writes(self) -> int:
        with self._lock:
            n = len(self._written)
            for nid in self._written:
                self._nodes.pop(nid, None)
                self._out.pop(nid, None)
            self._written.clear()
        return n

    # ── execution ──────────────────────────────────────────────────────────────
    def _khop(self, start: int, depth: int) -> int:
        frontier = {start}
        for _ in range(depth):
            nxt: set[int] = set()
            for u in frontier:
                nxt.update(self._out.get(u, ()))
            frontier = nxt
            if not frontier:
                break
        return len(frontier)

    def execute(self, worker_id: int, query: str, params: dict, timeout: float) -> list[dict]:
        """Dispatch on the canonical query text so the mock answers the REAL workloads.

        Matching on text rather than reimplementing a Cypher parser keeps the mock
        honest: if a query in the registry changes, the mock stops recognising it and
        the smoke test fails loudly instead of silently testing something else.
        """
        if query == "RETURN 1 AS ok":
            return [{"ok": 1}]
        if query == W.COUNT_NODES:
            return [{"n": len(self._nodes)}]
        if query == W.COUNT_RELS:
            return [{"n": sum(len(v) for v in self._out.values())}]
        if query == W.CLEANUP:
            self.cleanup_writes()
            return []

        if query == W.QUERIES["lookup_point"]:
            self._spend(0)
            n = self._nodes.get(params["paper_id"])
            return [{"id": n["id"], "year": n["pub_year"]}] if n else []

        if query == W.QUERIES["lookup_indexed"]:
            self._spend(0)
            return [{"n": len(self._by_date.get(params["pub_date"], ()))}]

        for depth in (1, 2, 3):
            if query == W.QUERIES[f"traversal_{depth}hop"]:
                self._spend(depth)
                return [{"n": self._khop(params["paper_id"], depth)}]

        if query == W.QUERIES["aggregation_by_year"]:
            self._spend(2)
            agg: dict[int, int] = defaultdict(int)
            for n in self._nodes.values():
                agg[n["pub_year"]] += 1
            return [{"year": y, "n": agg[y]} for y in sorted(agg)]

        if query == W.QUERIES["write_insert_citation"]:
            self._spend(1)
            new_id = params["new_id"]
            with self._lock:
                self._nodes[new_id] = {
                    "id": new_id, "pub_date": params["pub_date"], "pub_year": params["pub_year"],
                }
                self._out[new_id].append(params["paper_id"])
                self._written.add(new_id)
            return [{"id": new_id}]

        raise RuntimeError(f"mock adapter does not implement this query: {query[:80]!r}")

    def counts(self) -> dict:
        return {"nodes": len(self._nodes), "relationships": sum(len(v) for v in self._out.values())}

    def footprint(self) -> dict:
        return {
            "observable": True,
            "probes": {"mock_resident_nodes": len(self._nodes)},
            "note": "mock engine - never publish these numbers",
        }
