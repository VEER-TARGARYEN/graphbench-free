#!/usr/bin/env python3
"""Hour-zero gate: prove every platform is reachable and find out what it can actually do.

Run this FIRST, before loading data or writing a line of analysis. A platform that fails
here is excluded in hour one - when exclusion is a documented result - rather than in
hour thirty, when it is a catastrophe. FalkorDB Cloud is the specific worry: its Bolt
support is documented as experimental and its free-tier page documents RESP, not Bolt.

The capability matrix is not incidental. CognoDB publishes no feature matrix and has no
documentation site, so what its Cypher accepts has to be established empirically. That
matrix is a genuinely useful README section that no other candidate will have, and it
tells you which workloads are safe to write before you write them.

    python scripts/probe_platforms.py --all
    python scripts/probe_platforms.py --only cognodb
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import config as cfg  # noqa: E402
from harness import environment as envmod  # noqa: E402

# Each probe is (name, cypher, why-it-matters). A failure is information, not an error:
# it tells us which workloads are safe to write and what to declare as a caveat.
CAPABILITY_PROBES: list[tuple[str, str, str]] = [
    ("basic_return", "RETURN 1 AS n", "Bolt handshake and Cypher parsing"),
    ("parameters", "RETURN $x AS n", "parameterised queries - mandatory, never concatenate"),
    ("unwind_create", "UNWIND $rows AS r RETURN r.id AS id", "the batching construct every load depends on"),
    ("count_distinct", "UNWIND [1,1,2] AS x RETURN count(DISTINCT x) AS n", "the k-hop metric's aggregate"),
    ("order_by", "UNWIND [3,1,2] AS x RETURN x ORDER BY x", "aggregation workload ordering"),
    ("explain", "EXPLAIN RETURN 1", "plan inspection for root-cause analysis"),
    ("profile", "PROFILE RETURN 1", "profiled execution for root-cause analysis"),
    ("fixed_len_pattern", "MATCH (a)-[:NOPE]->(b)-[:NOPE]->(c) RETURN count(*) AS n",
     "fixed-length multi-hop - the traversal metric"),
    ("var_len_pattern", "MATCH (a)-[:NOPE*1..2]->(b) RETURN count(*) AS n",
     "variable-length paths - NOT used by this benchmark, probed only to document support"),
    ("show_indexes", "SHOW INDEXES", "index introspection for the fairness ledger"),
    ("dbms_components", "CALL dbms.components() YIELD name RETURN name", "server identity"),
    ("apoc_present", "RETURN apoc.version() AS v", "APOC availability (expected absent on managed tiers)"),
    ("gds_present", "CALL gds.version() YIELD gdsVersion RETURN gdsVersion",
     "graph algorithms (CognoDB documents these as absent)"),
]

LATENCY_SAMPLES = 20


def probe_one(platform: dict, timeout: float) -> dict:
    out: dict = {
        "id": platform["id"],
        "label": platform["label"],
        "track": platform.get("track"),
        "enabled": platform.get("enabled", False),
        "advertised": platform.get("advertised", {}),
        "reachable": False,
        "capabilities": {},
    }

    try:
        adapter = cfg.build_adapter(platform)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    try:
        adapter.connect()
    except Exception as e:
        out["error"] = f"connect failed - {type(e).__name__}: {e}"
        return out

    try:
        info = adapter.verify()
        out["reachable"] = info.get("reachable", False)
        out["server"] = {k: v for k, v in info.items() if k != "reachable"}
    except Exception as e:
        out["error"] = f"verify failed - {type(e).__name__}: {e}"
        adapter.close()
        return out

    # Baseline round-trip. This is the floor under every latency number the platform
    # will ever produce, and on a managed tier it is mostly network. Reporting it lets
    # a reader separate "this engine is slow" from "this endpoint is far away".
    samples = []
    for _ in range(LATENCY_SAMPLES):
        t0 = time.perf_counter()
        try:
            adapter.execute(0, "RETURN 1 AS n", {}, timeout=timeout)
            samples.append((time.perf_counter() - t0) * 1000)
        except Exception:
            break
    if samples:
        samples.sort()
        out["baseline_rtt_ms"] = {
            "samples": len(samples),
            "min": round(samples[0], 3),
            "p50": round(statistics.median(samples), 3),
            "p95": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 3),
            "max": round(samples[-1], 3),
        }

    for name, query, why in CAPABILITY_PROBES:
        params = {"x": 1, "rows": [{"id": 1}]}
        try:
            adapter.execute(0, query, params, timeout=timeout)
            out["capabilities"][name] = {"supported": True, "why": why}
        except Exception as e:
            out["capabilities"][name] = {
                "supported": False,
                "why": why,
                "error": f"{type(e).__name__}",
                "detail": str(e).split("\n")[0][:200],
            }

    try:
        out["counts"] = adapter.counts()
    except Exception as e:
        out["counts"] = {"error": type(e).__name__}

    out["footprint"] = adapter.footprint()
    adapter.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="platform ids to probe")
    ap.add_argument("--all", action="store_true", help="include platforms marked enabled: false")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    cfg.load_env()
    platforms = cfg.selected_platforms(only=args.only, include_disabled=args.all or bool(args.only))
    if not platforms:
        print("no platforms selected. Enable one in config/platforms.yaml or pass --all.", file=sys.stderr)
        return 2

    results = []
    for p in platforms:
        print(f"\n=== {p['label']}  ({p['id']}, track {p.get('track')}) ===")
        r = probe_one(p, args.timeout)
        results.append(r)

        if not r["reachable"]:
            print(f"  UNREACHABLE  {r.get('error', 'unknown')}")
            print("  -> exclude this platform and report the exclusion as a result.")
            continue

        rtt = r.get("baseline_rtt_ms", {})
        print(f"  reachable    baseline RTT p50 {rtt.get('p50', '?')} ms  (p95 {rtt.get('p95', '?')} ms)")
        counts = r.get("counts", {})
        if "nodes" in counts:
            print(f"  contents     {counts['nodes']:,} nodes / {counts['relationships']:,} relationships")
        unsupported = [k for k, v in r["capabilities"].items() if not v["supported"]]
        supported = [k for k, v in r["capabilities"].items() if v["supported"]]
        print(f"  supports     {', '.join(supported) or '(none)'}")
        print(f"  missing      {', '.join(unsupported) or '(none)'}")
        fp = r.get("footprint", {})
        print(f"  footprint    {'observable' if fp.get('observable') else 'NOT observable'}")

    env = envmod.capture()
    print()
    print(f"ENVIRONMENT  {envmod.summarise(env)}")

    cfg.RESULTS.mkdir(parents=True, exist_ok=True)
    out = cfg.RESULTS / "probe.json"
    out.write_text(
        json.dumps({"environment": env, "platforms": results}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {out.relative_to(cfg.ROOT)}")

    ok = [r for r in results if r["reachable"]]
    print(f"{len(ok)}/{len(results)} platforms reachable")
    if len(ok) < len(results):
        print("Unreachable platforms are a RESULT. Record them in the README with the error text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
