"""Bolt/Cypher adapter - drives CognoDB, Neo4j, Memgraph, ArcadeDB and FalkorDB.

One adapter for five engines is not a shortcut, it is the fairness argument: the same
client library, the same connection-pool settings, the same query text and the same
parameters reach every platform, so a latency difference cannot be blamed on the client.
CognoDB, Neo4j, Memgraph and ArcadeDB all speak Bolt and accept the official driver
unmodified. FalkorDB's Bolt support is documented as experimental, which is exactly why
`verify()` exists and why the platform is disabled until it passes.

Per-thread sessions: the driver's Session is NOT thread-safe, so each worker gets its
own from a threading.local. Pool size is fixed identically across platforms in
config/platforms.yaml - connection-pool asymmetry is a benchmarking crime.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Sequence

import neo4j
from neo4j import GraphDatabase, Query
from neo4j.exceptions import ClientError, Neo4jError

from ..workloads import cit_hepph as W
from .base import Adapter, LoadPhase, LoadReport


class BoltAdapter(Adapter):
    def __init__(self, platform_id: str, label: str, config: dict):
        super().__init__(platform_id, label, config)
        self.dialect = config.get("dialect", self._infer_dialect(platform_id))
        self._driver: neo4j.Driver | None = None
        self._local = threading.local()
        self._sessions: list[neo4j.Session] = []
        self._sessions_lock = threading.Lock()
        self._database = config.get("database") or None

    @staticmethod
    def _infer_dialect(platform_id: str) -> str:
        pid = platform_id.lower()
        if "memgraph" in pid:
            return "memgraph"
        if "falkor" in pid:
            return "falkordb"
        return "default"

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def _resolve_uri(self) -> tuple[str, tuple[str, str]]:
        env = self.config["env"]
        uri = os.environ.get(env["uri"], "").strip()
        user = os.environ.get(env["user"], "").strip()
        password = os.environ.get(env["password"], "")
        if not uri:
            raise RuntimeError(
                f"{self.platform_id}: missing {env['uri']} in the environment. "
                f"Copy .env.example to .env and fill it in - credentials are never committed."
            )
        if self.config.get("tls_insecure") and "+s://" in uri:
            # Self-signed certificates (Memgraph Cloud) - '+ssc' means encrypted but
            # certificate not verified. Preferred over a global trust override because
            # it stays scoped to the one platform that needs it, and it is visible.
            uri = uri.replace("+s://", "+ssc://")
        # Self-hosted engines frequently ship with authentication disabled - Memgraph
        # Community and a default FalkorDB container both do. The driver wants auth=None
        # for those, not an empty tuple, which it would try to send as a credential.
        auth = (user, password) if user else None
        return uri, auth

    def connect(self) -> None:
        uri, auth = self._resolve_uri()
        d = self.config.get("bolt_defaults", {})
        self._driver = GraphDatabase.driver(
            uri,
            auth=auth,
            max_connection_pool_size=d.get("max_connection_pool_size", 50),
            connection_acquisition_timeout=d.get("connection_acquisition_timeout", 60),
            max_transaction_retry_time=d.get("max_transaction_retry_time", 30),
        )

    def _session(self) -> neo4j.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            assert self._driver is not None, "connect() first"
            s = self._driver.session(database=self._database) if self._database else self._driver.session()
            self._local.session = s
            with self._sessions_lock:
                self._sessions.append(s)
        return s

    def close(self) -> None:
        with self._sessions_lock:
            for s in self._sessions:
                try:
                    s.close()
                except Exception:
                    pass
            self._sessions.clear()
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def verify(self) -> dict:
        """Prove the endpoint speaks Bolt and report whatever it discloses about itself."""
        info: dict = {"reachable": False}
        t0 = time.perf_counter()
        rows = self.execute(0, "RETURN 1 AS ok", {}, timeout=15)
        info["reachable"] = bool(rows) and rows[0].get("ok") == 1
        info["round_trip_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        # Server identity is optional everywhere and absent on several engines.
        # Absence is recorded, never inferred.
        for label, q in (
            ("components", "CALL dbms.components() YIELD name, versions, edition "
                           "RETURN name, versions, edition"),
            ("version", "RETURN 1"),
        ):
            try:
                info[label] = self.execute(0, q, {}, timeout=10)
                break
            except Exception as e:
                info.setdefault("identity_errors", []).append(f"{label}: {type(e).__name__}")
        return info

    # ── data ───────────────────────────────────────────────────────────────────
    def wipe(self) -> None:
        # Chunked delete: a single unbounded DETACH DELETE will OOM a 512 MB instance
        # long before it finishes, which would look like an engine defect and is ours.
        while True:
            rows = self.execute(
                0,
                "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(n) AS n",
                {},
                timeout=120,
            )
            if not rows or not rows[0].get("n"):
                break

    def _build_index(self, kind: str, report: LoadReport) -> float:
        """Try the dialect's DDL, then documented fallbacks.

        Silently running unindexed would make a platform look catastrophically slow for
        a reason that is our fault, not theirs - so a total failure is recorded as an
        error and surfaced in the results rather than swallowed.
        """
        candidates = list(W.DIALECTS.get(self.dialect, {}).get(kind, []))
        for fb in W.INDEX_FALLBACKS.get(kind, []):
            if fb not in candidates:
                candidates.append(fb)

        t0 = time.perf_counter()
        for ddl in candidates:
            try:
                self.execute(0, ddl, {}, timeout=600)
                report.index_ddl.append(f"{kind}: {ddl}")
                return time.perf_counter() - t0
            except (ClientError, Neo4jError) as e:
                msg = str(e)
                if "equivalent" in msg.lower() or "already exists" in msg.lower():
                    report.index_ddl.append(f"{kind}: {ddl}  [already present]")
                    return time.perf_counter() - t0
                continue
            except Exception:
                continue
        report.errors.append(f"{kind}: no index DDL accepted; workloads ran UNINDEXED")
        return time.perf_counter() - t0

    def load(self, nodes: Sequence[dict], edges: Sequence[dict], batch_size: int) -> LoadReport:
        report = LoadReport()

        def batched(rows: Sequence[dict], query: str, phase: str) -> None:
            t0 = time.perf_counter()
            done = 0
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                self.execute(0, query, {"rows": list(chunk)}, timeout=300)
                done += len(chunk)
            report.phases.append(LoadPhase(phase, time.perf_counter() - t0, done))

        batched(nodes, W.LOAD_NODES, "load_nodes")

        # The :Paper(id) index must exist BEFORE edges load, or every edge costs a full
        # scan and the relationship rate measures our mistake instead of the engine.
        report.phases.append(LoadPhase("index_id", self._build_index("index_id", report), 1))

        batched(edges, W.LOAD_EDGES, "load_edges")

        report.phases.append(
            LoadPhase("index_secondary", self._build_index("index_secondary", report), 1)
        )
        return report

    def cleanup_writes(self) -> int:
        rows = self.execute(0, "MATCH (n:BenchWrite) RETURN count(n) AS n", {}, timeout=120)
        n = rows[0]["n"] if rows else 0
        if n:
            self.execute(0, W.CLEANUP, {}, timeout=600)
        return int(n)

    # ── execution ──────────────────────────────────────────────────────────────
    def execute(self, worker_id: int, query: str, params: dict, timeout: float) -> list[dict]:
        session = self._session()
        # Auto-commit with a server-side timeout attached to the Query object. An
        # explicit BEGIN/COMMIT would add a round trip to every measurement; identical
        # across platforms, but it would inflate every latency for no benefit.
        result = session.run(Query(query, timeout=timeout), params)
        return [dict(r) for r in result]

    def counts(self) -> dict:
        n = self.execute(0, W.COUNT_NODES, {}, timeout=300)
        r = self.execute(0, W.COUNT_RELS, {}, timeout=300)
        return {
            "nodes": int(n[0]["n"]) if n else 0,
            "relationships": int(r[0]["n"]) if r else 0,
        }

    # ── observability ──────────────────────────────────────────────────────────
    def footprint(self) -> dict:
        probes = W.FOOTPRINT_PROBES.get(self.dialect) or W.FOOTPRINT_PROBES["default"]
        out: dict = {"observable": False, "probes": {}}
        for name, q in probes:
            try:
                out["probes"][name] = self.execute(0, q, {}, timeout=30)
                out["observable"] = True
            except Exception as e:
                out["probes"][name] = {"error": type(e).__name__}
        if not out["observable"]:
            # This is the honest answer the brief asks for, and the asymmetry between
            # engines that expose a memory figure and engines that do not is a finding.
            out["reason"] = "platform exposes no queryable size or memory metric over Bolt"
        return out
