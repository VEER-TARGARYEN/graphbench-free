#!/usr/bin/env python3
"""End-to-end harness self-test. No network, no credentials, no database.

Proves, against two mock engines with deliberately different performance profiles:

  1. load + post-load count assertion
  2. cross-engine result equivalence (agreement AND detected divergence)
  3. open-loop scheduled dispatch under concurrency
  4. HdrHistogram percentile recording and merge across workers
  5. the LDBC 95%-on-time validity gate actually FIRING on a throttled engine
  6. coordinated-omission delta - corrected p99 exceeding uncorrected p99

Point 5 is the one that matters. A validity gate that has never been observed to fail
is not a gate, it is a comment.

    python scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import equivalence as eq  # noqa: E402
from harness.adapters.mock import MockAdapter  # noqa: E402
from harness.scheduler import Op, as_ops, build_schedule, run_open_loop  # noqa: E402
from harness.workloads import cit_hepph as W  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def tiny_graph(n_nodes: int = 400, fan: int = 4) -> tuple[list[dict], list[dict]]:
    nodes = [
        {"id": i, "d": f"199{2 + i % 8}-0{1 + i % 9}-1{i % 9}", "y": 1992 + i % 8}
        for i in range(n_nodes)
    ]
    edges = [
        {"s": i, "t": (i * 7 + k * 13 + 1) % n_nodes}
        for i in range(n_nodes)
        for k in range(fan)
    ]
    # Deduplicate: the real loader receives a deduplicated edge list.
    seen, uniq = set(), []
    for e in edges:
        key = (e["s"], e["t"])
        if key not in seen and e["s"] != e["t"]:
            seen.add(key)
            uniq.append(e)
    return nodes, uniq


def main() -> int:
    nodes, edges = tiny_graph()
    print(f"synthetic graph: {len(nodes)} nodes / {len(edges)} edges\n")

    fast = MockAdapter("mock_fast", "Mock (fast)", {"simulate": {"base_ms": 1.0, "jitter_ms": 0.5, "per_hop_ms": 0.5}})
    # Throttles hard after 40 operations - a burstable instance draining its credits.
    # The factor is chosen so the throttled SERVICE rate (workers / latency) falls below
    # the OFFERED rate: 8 workers at ~300 ms is ~27 ops/s against 60 ops/s offered.
    # Without that inequality no queue forms, nothing arrives late, and the on-time gate
    # correctly reports a pass - which is what a milder slowdown genuinely means.
    slow = MockAdapter("mock_throttled", "Mock (throttled)", {
        "simulate": {"base_ms": 1.0, "jitter_ms": 0.5, "per_hop_ms": 0.5,
                     "throttle_after": 40, "throttle_factor": 150.0}
    })

    print("1. load")
    for a in (fast, slow):
        a.connect()
        rep = a.load(nodes, edges, batch_size=1000).to_dict()
        c = a.counts()
        check(f"{a.platform_id}: counts match after load",
              c["nodes"] == len(nodes) and c["relationships"] == len(edges),
              f"{c['nodes']} nodes / {c['relationships']} rels")
        check(f"{a.platform_id}: ingest rates reported",
              rep["nodes_per_sec"] is not None and rep["relationships_per_sec"] is not None)
        check(f"{a.platform_id}: index DDL recorded", len(rep["index_ddl"]) == 2)

    print("\n2. cross-engine equivalence")
    table: dict[str, dict] = {}
    for wid in ("lookup_point", "traversal_1hop", "traversal_2hop", "traversal_3hop",
                "lookup_indexed", "aggregation_by_year"):
        params = ({"paper_id": 7} if "paper_id" in W.QUERIES[wid] else
                  {"pub_date": nodes[7]["d"]} if "pub_date" in W.QUERIES[wid] else {})
        ordered = "ORDER BY" in W.QUERIES[wid].upper()
        for a in (fast, slow):
            rows = a.execute(0, W.QUERIES[wid], params, timeout=10)
            table.setdefault(wid, {})[a.platform_id] = eq.checksum(rows, ordered=ordered)
    report = eq.compare(table)
    check("identical engines agree on every workload", not report["diverge"], report["verdict"])

    # Now inject a real divergence and confirm the comparator catches it, rather than
    # trusting a gate that has only ever been shown the happy path.
    bad = dict(table["traversal_2hop"])
    bad["mock_divergent"] = eq.checksum([{"n": 999999}])
    div = eq.compare({**table, "traversal_2hop": bad})
    check("comparator detects an injected divergence", "traversal_2hop" in div["diverge"])

    print("\n3. open-loop dispatch + percentiles + validity gate")
    pool = [{"paper_id": i} for i in range(200)]
    results = {}
    for a in (fast, slow):
        n_ops, rate, workers = 200, 60.0, 8
        stream = [("traversal_2hop", pool[i % len(pool)]) for i in range(n_ops)]
        ops = as_ops(stream, build_schedule(n_ops, rate, arrival="uniform"))

        def execute(_wid: int, op: Op, _a=a):
            return _a.execute(0, W.QUERIES[op.workload_id], op.params, timeout=10)

        rec = run_open_loop(ops, workers=workers, execute=execute,
                            on_time_tolerance_sec=1.0, per_query_timeout_sec=10.0)
        s = rec.summary(on_time_threshold_pct=95.0)
        results[a.platform_id] = s
        ls = s["latency_stats"]
        print(f"    {a.platform_id:<16} n={s['count']:>4}  p50 {ls['p50']:>8.2f} ms  "
              f"p95 {ls['p95']:>8.2f} ms  p99 {ls['p99']:>8.2f} ms  "
              f"on-time {s['on_time_pct']:>5.1f}%  valid={s['valid']}")

    f, s = results["mock_fast"], results["mock_throttled"]
    check("all operations recorded", f["count"] == 200 and s["count"] == 200,
          f"{f['count']} / {s['count']}")
    check("percentiles are monotonic",
          f["latency_stats"]["p50"] <= f["latency_stats"]["p95"] <= f["latency_stats"]["p99"])
    check("fast engine passes the on-time gate", f["valid"], f"{f['on_time_pct']}% on time")
    check("THROTTLED engine FAILS the on-time gate", not s["valid"],
          f"{s['on_time_pct']}% on time - this is the gate doing its job")
    check("throttled engine is measurably slower at p99",
          s["latency_stats"]["p99"] > f["latency_stats"]["p99"] * 2,
          f"{s['latency_stats']['p99']:.1f} ms vs {f['latency_stats']['p99']:.1f} ms")

    print("\n4. coordinated-omission correction")
    co = s["coordinated_omission_delta_ms"]
    for p in ("p95", "p99"):
        d = co[p]
        print(f"    {p}: corrected {d['corrected']:>9.2f} ms   "
              f"uncorrected {d['uncorrected']:>9.2f} ms   hidden {d['delta']:>9.2f} ms")
    check("corrected tail exceeds uncorrected on the throttled engine",
          co["p99"]["delta"] > 0,
          f"a closed-loop harness would have hidden {co['p99']['delta']:.0f} ms at p99")
    from harness.recorder import decode_buckets, percentile_from_buckets
    buckets = decode_buckets(s["histogram"])
    recomputed = percentile_from_buckets(buckets, 99) / 1000.0
    check("embedded histogram round-trips", sum(c for _, c in buckets) == s["count"],
          f"{len(buckets)} buckets, {len(s['histogram'])} b64 chars")
    # Tolerance is RELATIVE, not absolute. HdrHistogram stores 3 significant figures, so
    # a bucket near 4,400 ms is ~4 ms wide and the recomputed value can legitimately
    # differ by one bucket. Asserting an absolute millisecond tolerance would be
    # asserting a precision the data structure never promised.
    reported = s["latency_stats"]["p99"]
    rel_err = abs(recomputed - reported) / reported if reported else 0.0
    check("p99 recomputed from the embedded histogram matches the reported p99",
          rel_err <= 0.001,
          f"{recomputed:.2f} ms vs {reported:.2f} ms  ({rel_err * 100:.3f}% - within 3-sig-fig precision)")

    print("\n5. write cleanup")
    for i in range(10):
        fast.execute(0, W.QUERIES["write_insert_citation"],
                     {"paper_id": 1, "new_id": 900000000 + i, "pub_date": "2026-01-01", "pub_year": 2026},
                     timeout=10)
    before = fast.counts()["nodes"]
    removed = fast.cleanup_writes()
    after = fast.counts()["nodes"]
    check("written nodes are removed so runs start identically",
          removed == 10 and after == before - 10, f"removed {removed}, {before} -> {after}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for f_ in FAILURES:
            print(f"  - {f_}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
