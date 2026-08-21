#!/usr/bin/env python3
"""Run the benchmark suite against one or more platforms.

    python scripts/run_benchmark.py --dry-run                 # print the matrix + time estimate
    python scripts/run_benchmark.py --only cognodb --phases load,equivalence,read,mixed
    python scripts/run_benchmark.py --only cognodb --phases read --warmup hot

MATRIX DISCIPLINE. The commonest way this assignment fails is discovering in hour 40
that the run matrix needs a week. `--dry-run` multiplies it out and prints an estimate
before anything connects. Read-latency workloads run at concurrency 1 (they are latency
metrics); the concurrency sweep belongs to the mixed workload, which is the throughput
metric. Conflating the two multiplies the matrix by 3 for no additional rubric credit.

RUN ORDER. Platforms are interleaved, not run in contiguous blocks. A contiguous block
bakes in both time-of-day effects and burstable CPU-credit state, so every platform
after the first runs under different conditions than the first.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import config as cfg  # noqa: E402
from harness import equivalence as eq  # noqa: E402
from harness.scheduler import Op, as_ops, build_schedule, run_open_loop, weighted_op_stream  # noqa: E402
from harness.workloads import cit_hepph as W  # noqa: E402

WRITE_ID_BASE = 900_000_000  # synthetic ids for the write workload, far above any real paper id


# ── loading params & data ──────────────────────────────────────────────────────
def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def param_pool(spec: dict, curated: bool = True) -> list[dict]:
    """Materialise a workload's frozen parameter list."""
    name = spec.get("params")
    field = spec.get("param_field")
    if not name or not field:
        return [{}]
    if not curated and name.startswith("start_nodes_"):
        name = name.replace(".csv", "_uncurated.csv")
    rows = read_csv(cfg.PARAMS / name)
    out = []
    for r in rows:
        v = r[field]
        out.append({field: int(v) if v.lstrip("-").isdigit() else v})
    return out


def load_dataset(scale: str) -> tuple[list[dict], list[dict]]:
    nodes = [
        {"id": int(r["paper_id"]), "d": r["pub_date"], "y": int(r["pub_year"])}
        for r in read_csv(cfg.DATA / scale / "nodes.csv")
    ]
    edges = [{"s": int(r["src"]), "t": int(r["dst"])} for r in read_csv(cfg.DATA / scale / "edges.csv")]
    return nodes, edges


# ── phases ─────────────────────────────────────────────────────────────────────
def phase_load(adapter, wl: dict, scale: str, wipe: bool) -> dict:
    nodes, edges = load_dataset(scale)
    print(f"    loading {len(nodes):,} nodes / {len(edges):,} edges "
          f"(batch {wl['ingest']['batch_size']}, UNWIND over the driver)")
    if wipe:
        print("    wiping ...", end="", flush=True)
        adapter.wipe()
        print(" done")
    report = adapter.load(nodes, edges, wl["ingest"]["batch_size"])
    d = report.to_dict()
    print(f"    ingest  {d['nodes_per_sec']:,.0f} nodes/s  {d['relationships_per_sec']:,.0f} rels/s  "
          f"total {d['total_wall_clock_sec']:.1f}s (incl. index build)")
    for e in d["errors"]:
        print(f"    WARN    {e}")
    observed = adapter.counts()
    d["observed_counts"] = observed
    d["expected_counts"] = {"nodes": len(nodes), "relationships": len(edges)}
    d["counts_match"] = (
        observed["nodes"] == len(nodes) and observed["relationships"] == len(edges)
    )
    if not d["counts_match"]:
        print(f"    WARN    post-load counts differ: {observed} != "
              f"{{'nodes': {len(nodes)}, 'relationships': {len(edges)}}}")
    return d


def phase_equivalence(adapter, wl: dict) -> dict:
    """Run every read query once and checksum the result.

    This runs BEFORE any timing. A latency number for a query that returns different
    rows on different engines is not a comparison, it is a coincidence.
    """
    out: dict = {}
    for spec in wl["workloads"]:
        if not spec.get("read"):
            continue
        wid = spec["id"]
        pool = param_pool(spec)
        params = pool[0]
        ordered = "ORDER BY" in W.QUERIES[wid].upper()
        try:
            rows = adapter.execute(0, W.QUERIES[wid], params, timeout=wl["protocol"]["timeouts"]["per_query_sec"])
            out[wid] = {**eq.checksum(rows, ordered=ordered), "params": params}
        except Exception as e:
            out[wid] = {"error": f"{type(e).__name__}: {str(e).splitlines()[0][:200]}", "params": params}
    ok = sum(1 for v in out.values() if "sha256" in v)
    print(f"    equivalence  {ok}/{len(out)} read workloads produced a checksum")
    return out


def _run_workload(adapter, wl: dict, wid: str, pool: list[dict], *, iterations: int,
                  workers: int, rate: float | None, warmup_ops: int, label: str) -> dict:
    proto = wl["protocol"]
    timeout = proto["timeouts"]["per_query_sec"]
    query = W.QUERIES[wid]

    if warmup_ops:
        for i in range(warmup_ops):
            try:
                adapter.execute(0, query, pool[i % len(pool)], timeout=timeout)
            except Exception:
                pass

    stream = [(wid, pool[i % len(pool)]) for i in range(iterations)]
    offsets = build_schedule(iterations, rate, arrival="uniform")
    ops = as_ops(stream, offsets)

    def execute(worker_id: int, op: Op):
        return adapter.execute(worker_id, query, op.params, timeout)

    t0 = time.perf_counter()
    rec = run_open_loop(
        ops,
        workers=workers,
        execute=execute,
        on_time_tolerance_sec=proto["on_time_tolerance_sec"],
        per_query_timeout_sec=timeout,
    )
    elapsed = time.perf_counter() - t0

    summary = rec.summary(on_time_threshold_pct=proto["on_time_threshold_pct"])
    summary.update({
        "workload": wid,
        "variant": label,
        "num_workers": workers,
        "target_rate": rate,
        "duration_sec": round(elapsed, 3),
        "throughput_qps": round(summary["count"] / elapsed, 2) if elapsed > 0 else 0.0,
    })
    ls = summary["latency_stats"]
    flag = "" if summary["valid"] else "  *** INVALID (on-time gate) ***"
    print(f"      {label:<26} p50 {ls.get('p50', 0):>8.2f} ms  p95 {ls.get('p95', 0):>8.2f} ms  "
          f"{summary['throughput_qps']:>7.1f} q/s  on-time {summary['on_time_pct']:>5.1f}%"
          f"  err {summary['errors']}{flag}")
    return summary


def phase_read(adapter, wl: dict, warmup_modes: list[str], iterations: int,
               include_uncurated: bool) -> list[dict]:
    proto = wl["protocol"]
    modes = {m["id"]: m for m in proto["warmup_modes"]}
    rate = proto["target_rates"].get(1)
    out: list[dict] = []

    for mode_id in warmup_modes:
        mode = modes[mode_id]
        print(f"    warm-up mode: {mode_id} ({mode['description']})")
        for spec in wl["workloads"]:
            if not spec.get("read"):
                continue
            wid = spec["id"]
            res = _run_workload(
                adapter, wl, wid, param_pool(spec),
                iterations=iterations, workers=1, rate=rate,
                warmup_ops=mode["warmup_ops"], label=f"{wid} [{mode_id}]",
            )
            res["warmup_mode"] = mode_id
            res["parameters"] = "curated"
            out.append(res)

            # The flagship chart: the same workload driven by uniform-random start
            # nodes instead of curated ones. Overlaying the two latency distributions
            # is what proves the curation mattered.
            if include_uncurated and wid == "traversal_3hop" and mode_id == "hot":
                res_u = _run_workload(
                    adapter, wl, wid, param_pool(spec, curated=False),
                    iterations=iterations, workers=1, rate=rate,
                    warmup_ops=mode["warmup_ops"], label=f"{wid} [uncurated]",
                )
                res_u["warmup_mode"] = mode_id
                res_u["parameters"] = "uncurated"
                out.append(res_u)
    return out


def phase_mixed(adapter, wl: dict, concurrency: list[int]) -> list[dict]:
    proto, mixed = wl["protocol"], wl["mixed"]
    timeout = proto["timeouts"]["per_query_sec"]
    specs = {s["id"]: s for s in wl["workloads"]}
    pools = {wid: param_pool(specs[wid]) for wid in mixed["weights"] if wid in specs}
    out: list[dict] = []

    print(f"    mixed workload: {mixed['id']}  weights={mixed['weights']}  "
          f"{mixed['duration_sec']}s per level, {mixed['arrival']} arrivals")

    for workers in concurrency:
        rate = proto["target_rates"].get(workers) or proto["target_rates"].get(str(workers))
        total = int((rate or 200) * mixed["duration_sec"])
        stream = weighted_op_stream(total, mixed["weights"], pools, seed=proto["ordering"]["randomize_seed"])

        # Synthetic ids assigned by sequence, so the exact same write stream is replayed
        # against every platform rather than depending on a live RNG.
        for i, (wid, params) in enumerate(stream):
            if wid == "write_insert_citation":
                params = dict(params)
                params.update({"new_id": WRITE_ID_BASE + i, "pub_date": "2026-01-01", "pub_year": 2026})
                stream[i] = (wid, params)

        offsets = build_schedule(total, rate, arrival=mixed["arrival"],
                                 seed=proto["ordering"]["randomize_seed"])
        ops = as_ops(stream, offsets)

        def execute(worker_id: int, op: Op):
            return adapter.execute(worker_id, W.QUERIES[op.workload_id], op.params, timeout)

        t0 = time.perf_counter()
        rec = run_open_loop(
            ops, workers=workers, execute=execute,
            on_time_tolerance_sec=proto["on_time_tolerance_sec"],
            per_query_timeout_sec=timeout,
            deadline_sec=mixed["duration_sec"],
        )
        elapsed = time.perf_counter() - t0
        s = rec.summary(on_time_threshold_pct=proto["on_time_threshold_pct"])
        s.update({
            "workload": mixed["id"], "variant": f"mixed@{workers}",
            "num_workers": workers, "target_rate": rate,
            "duration_sec": round(elapsed, 3),
            "throughput_qps": round(s["count"] / elapsed, 2) if elapsed > 0 else 0.0,
            "weights": mixed["weights"],
        })
        ls = s["latency_stats"]
        flag = "" if s["valid"] else "  *** INVALID (on-time gate) ***"
        print(f"      {workers:>2} clients @ {rate} q/s   p50 {ls.get('p50', 0):>8.2f} ms  "
              f"p95 {ls.get('p95', 0):>8.2f} ms  {s['throughput_qps']:>7.1f} q/s  "
              f"on-time {s['on_time_pct']:>5.1f}%{flag}")
        out.append(s)

        removed = adapter.cleanup_writes()
        if removed:
            print(f"         cleaned up {removed:,} written nodes - next level starts from the same graph")
    return out


# ── estimation ─────────────────────────────────────────────────────────────────
def estimate(wl: dict, platforms: list[dict], phases: list[str], warmup: list[str],
             concurrency: list[int], iterations: int, include_uncurated: bool) -> None:
    proto = wl["protocol"]
    reads = [s for s in wl["workloads"] if s.get("read")]
    rate1 = proto["target_rates"].get(1) or 20

    per_platform = 0.0
    lines = []
    if "load" in phases:
        per_platform += 240
        lines.append("  load           ~4 min  (18k nodes + 150k edges over the driver, tier dependent)")
    if "equivalence" in phases:
        per_platform += 20
        lines.append(f"  equivalence    ~20 s   ({len(reads)} read workloads x 1 execution)")
    if "read" in phases:
        n = len(reads) * len(warmup) + (1 if include_uncurated else 0)
        secs = n * iterations / rate1
        per_platform += secs
        lines.append(f"  read           ~{secs/60:.0f} min  "
                     f"({len(reads)} workloads x {len(warmup)} warm-up modes x {iterations} iters @ {rate1}/s"
                     f"{' + 1 uncurated control' if include_uncurated else ''})")
    if "mixed" in phases:
        secs = len(concurrency) * wl["mixed"]["duration_sec"]
        per_platform += secs
        lines.append(f"  mixed          ~{secs/60:.0f} min  ({len(concurrency)} concurrency levels x "
                     f"{wl['mixed']['duration_sec']}s)")

    recov = proto["ordering"]["credit_recovery_sec"] * max(0, len(platforms) - 1)
    total = per_platform * len(platforms) + recov

    print("\nRUN MATRIX")
    for ln in lines:
        print(ln)
    print(f"  per platform   ~{per_platform/60:.0f} min")
    print(f"  platforms      {len(platforms)}  ({', '.join(p['id'] for p in platforms)})")
    print(f"  credit recovery{recov/60:>5.0f} min inserted between platform switches")
    print(f"  TOTAL          ~{total/3600:.1f} h of pure execution, before failures, retries and reloads")
    print("  Budget backwards from 48 h. If this does not fit, cut scale M and the second")
    print("  dataset first - never the open-loop driver or the curated parameters.\n")


# ── main ───────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="platform ids")
    ap.add_argument("--scale", default="S", choices=["S", "M"])
    ap.add_argument("--phases", default="load,equivalence,read,mixed")
    ap.add_argument("--warmup", default="cold,hot")
    ap.add_argument("--concurrency", default=None, help="override, e.g. 1,10,40")
    ap.add_argument("--iterations", type=int, default=None)
    ap.add_argument("--no-uncurated", action="store_true", help="skip the uncurated control run")
    ap.add_argument("--no-wipe", action="store_true", help="do not clear the database before loading")
    ap.add_argument("--dry-run", action="store_true", help="print the matrix and exit")
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    cfg.load_env()
    wl = cfg.load_workloads()
    proto = wl["protocol"]

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    warmup = [m.strip() for m in args.warmup.split(",") if m.strip()]
    concurrency = ([int(c) for c in args.concurrency.split(",")] if args.concurrency
                   else proto["concurrency_levels"])
    iterations = args.iterations or proto["iterations"]["read_default"]
    if iterations < proto["iterations"]["minimum"]:
        print(f"iterations {iterations} is below the declared minimum "
              f"{proto['iterations']['minimum']}", file=sys.stderr)
        return 2

    platforms = cfg.selected_platforms(only=args.only, include_disabled=bool(args.only))
    if not platforms:
        print("no platforms selected", file=sys.stderr)
        return 2

    # Randomised, seeded order so the sequence is reproducible but not the same
    # advantage-conferring order every time.
    random.Random(proto["ordering"]["randomize_seed"]).shuffle(platforms)

    estimate(wl, platforms, phases, warmup, concurrency, iterations, not args.no_uncurated)
    if args.dry_run:
        return 0

    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%S")
    outdir = cfg.RESULTS / run_id
    outdir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "run_id": run_id,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scale": args.scale,
        "phases": phases,
        "warmup_modes": warmup,
        "concurrency": concurrency,
        "iterations": iterations,
        "dataset": json.loads((cfg.PARAMS / "dataset.json").read_text(encoding="utf-8")),
        "curation": json.loads((cfg.PARAMS / "curation.json").read_text(encoding="utf-8")),
        "platform_order": [p["id"] for p in platforms],
        "platforms": {},
    }
    equivalence_table: dict[str, dict] = {}

    for i, p in enumerate(platforms):
        print(f"\n=== {p['label']}  ({p['id']}, track {p.get('track')}) ===")
        entry: dict = {
            "id": p["id"], "label": p["label"], "track": p.get("track"),
            "advertised": p.get("advertised", {}), "notes": p.get("notes"),
        }
        try:
            adapter = cfg.build_adapter(p)
            adapter.connect()
            entry["verify"] = adapter.verify()
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            print(f"  SKIPPED  {entry['error']}")
            manifest["platforms"][p["id"]] = entry
            continue

        try:
            if "load" in phases:
                entry["ingest"] = phase_load(adapter, wl, args.scale, wipe=not args.no_wipe)
            if "equivalence" in phases:
                entry["equivalence"] = phase_equivalence(adapter, wl)
                for wid, c in entry["equivalence"].items():
                    if "sha256" in c:
                        equivalence_table.setdefault(wid, {})[p["id"]] = c
            if "read" in phases:
                entry["read"] = phase_read(adapter, wl, warmup, iterations, not args.no_uncurated)
            if "mixed" in phases:
                entry["mixed"] = phase_mixed(adapter, wl, concurrency)
            entry["footprint"] = adapter.footprint()
        except KeyboardInterrupt:
            entry["error"] = "interrupted"
            adapter.close()
            manifest["platforms"][p["id"]] = entry
            break
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            print(f"  ERROR    {entry['error']}")
        finally:
            adapter.close()

        manifest["platforms"][p["id"]] = entry
        (outdir / f"{p['id']}.json").write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")

        if i < len(platforms) - 1 and proto["ordering"]["credit_recovery_sec"]:
            wait = proto["ordering"]["credit_recovery_sec"]
            print(f"\n  idling {wait}s before the next platform, so a drained CPU-credit bucket")
            print("  does not hand the next platform in the sequence an advantage.")
            time.sleep(wait)

    if equivalence_table:
        report = eq.compare(equivalence_table)
        manifest["equivalence_report"] = report
        print(f"\nEQUIVALENCE: {report['verdict']}")
        for wid, d in report["diverge"].items():
            print(f"  {wid}:")
            for g in d["groups"]:
                print(f"    {g['sha256']}  rows={g['row_count']}  {', '.join(g['platforms'])}")

    manifest["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {(outdir / 'manifest.json').relative_to(cfg.ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
